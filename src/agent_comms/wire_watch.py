"""Optional local file notification for idle wire owners.

The bus and registry remain authoritative. A notification only prompts a
normal read; the periodic polling recovers missed events and checks model
configuration that can change outside the wire directory.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
import ctypes
import os
import struct
import sys
from pathlib import Path

from .goal_waits import GoalWaits

_IN_MODIFY = 0x00000002
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO = 0x00000080
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_MASK = _IN_CLOSE_WRITE | _IN_MOVED_TO | _IN_DELETE_SELF | _IN_MOVE_SELF
_HEADER = struct.Struct("=iIII")
_AUTHORITY_FILES = {
    b"bus.jsonl",
    b"registry.json",
    b".registry-owner-guard",
    GoalWaits.filename.encode(),
}


@dataclass(frozen=True)
class WatchSource:
    """Files in one directory whose physical changes wake a reader."""

    directory: Path
    names: frozenset[bytes]
    modified: bool = False

    @classmethod
    def wire(cls, root: Path) -> WatchSource:
        return cls(root, _AUTHORITY_FILES)

    @classmethod
    def file(cls, path: Path) -> WatchSource:
        return cls(path.parent, frozenset({os.fsencode(path.name)}))


class WireWatch(ABC):
    """One change-driven observation lifetime, including notification loss."""

    @abstractmethod
    async def prepare(self) -> None: ...

    @abstractmethod
    async def wait(self) -> WireWatch: ...

    @abstractmethod
    def notify(self) -> None:
        """Wake for a process-local transition owned by the observer."""

    def close(self) -> None:
        pass

    @staticmethod
    async def observations(*sources: WatchSource) -> AsyncIterator[WireWatch]:
        watcher = open_wire_watcher(*sources)
        try:
            while True:
                await watcher.prepare()
                yield watcher
                watcher = await watcher.wait()
        finally:
            watcher.close()


class PollingWireWatch(WireWatch):
    async def prepare(self) -> None:
        await asyncio.sleep(0.05)

    async def wait(self) -> WireWatch:
        return self

    def notify(self) -> None:
        # This fallback already advances on its polling cadence.
        pass


class WireChangeWatch(WireWatch):
    def __init__(
        self, fd: int, loop: asyncio.AbstractEventLoop,
        names_by_watch: dict[int, frozenset[bytes]],
    ):
        self.names_by_watch = names_by_watch
        self.fd = fd
        self.loop = loop
        self.changed = asyncio.Event()
        self.invalid = False
        loop.add_reader(fd, self._on_ready)

    async def prepare(self) -> None:
        self.changed.clear()

    async def wait(self) -> WireWatch:
        if self.invalid:
            self.close()
            return PollingWireWatch()
        await self.changed.wait()
        return self

    def notify(self) -> None:
        self.changed.set()

    def _on_ready(self) -> None:
        try:
            while raw := os.read(self.fd, 65536):
                offset = 0
                while offset + _HEADER.size <= len(raw):
                    watch, mask, _, length = _HEADER.unpack_from(raw, offset)
                    offset += _HEADER.size
                    name = raw[offset : offset + length].partition(b"\0")[0]
                    offset += length
                    if name in self.names_by_watch.get(watch, ()) or mask & (
                        _IN_Q_OVERFLOW | _IN_IGNORED | _IN_DELETE_SELF | _IN_MOVE_SELF
                    ):
                        self.changed.set()
                    if mask & (_IN_IGNORED | _IN_DELETE_SELF | _IN_MOVE_SELF):
                        self.invalid = True
        except BlockingIOError:
            pass
        except OSError:
            self.invalid = True
            self.changed.set()

    def close(self) -> None:
        if self.fd >= 0:
            self.loop.remove_reader(self.fd)
            os.close(self.fd)
            self.fd = -1


def open_wire_watcher(*sources: WatchSource) -> WireWatch:
    """Use Linux notifications for every declared physical change source.

    A healthy native watcher reports queue loss and watched-directory removal,
    so it needs no periodic speculative read. Platforms or source directories
    without native notification use the existing polling owner.
    """
    if not sources:
        raise ValueError("Wire watcher requires at least one source")
    if sys.platform != "linux":
        return PollingWireWatch()
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        init = libc.inotify_init1
        add = libc.inotify_add_watch
    except (AttributeError, OSError):
        return PollingWireWatch()
    init.argtypes = [ctypes.c_int]
    init.restype = ctypes.c_int
    add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    add.restype = ctypes.c_int
    fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        return PollingWireWatch()
    grouped: dict[Path, tuple[frozenset[bytes], bool]] = {}
    for source in sources:
        names, modified = grouped.get(source.directory, (frozenset(), False))
        grouped[source.directory] = (names | source.names, modified or source.modified)
    names_by_watch = {}
    for directory, (names, modified) in grouped.items():
        watch = add(
            fd, os.fsencode(directory), _MASK | (_IN_MODIFY if modified else 0)
        )
        if watch < 0:
            os.close(fd)
            return PollingWireWatch()
        names_by_watch[watch] = names
    try:
        return WireChangeWatch(fd, asyncio.get_running_loop(), names_by_watch)
    except (NotImplementedError, OSError):
        os.close(fd)
        return PollingWireWatch()
