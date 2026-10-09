"""Optional local file notification for idle wire owners.

The bus and registry remain authoritative. A notification only prompts a
normal read; the periodic polling recovers missed events and checks model
configuration that can change outside the wire directory.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import suppress
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


class WireWatch(ABC):
    """One observation cadence and its resources, including notification loss."""

    @abstractmethod
    async def prepare(self) -> None: ...

    @abstractmethod
    async def wait(self) -> WireWatch: ...

    def close(self) -> None:
        pass

    @staticmethod
    async def observations(root: Path, names: frozenset[bytes] = _AUTHORITY_FILES,
                           *, modified: bool = False) -> AsyncIterator[None]:
        watcher = open_wire_watcher(root, names, modified=modified)
        try:
            while True:
                await watcher.prepare()
                yield None
                watcher = await watcher.wait()
        finally:
            watcher.close()


class PollingWireWatch(WireWatch):
    async def prepare(self) -> None:
        await asyncio.sleep(0.05)

    async def wait(self) -> WireWatch:
        return self


class WireChangeWatch(WireWatch):
    def __init__(self, fd: int, loop: asyncio.AbstractEventLoop, names: frozenset[bytes] = _AUTHORITY_FILES):
        self.names = names
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
        with suppress(TimeoutError):
            async with asyncio.timeout(1.0):
                await self.changed.wait()
        return self

    def _on_ready(self) -> None:
        try:
            while raw := os.read(self.fd, 65536):
                offset = 0
                while offset + _HEADER.size <= len(raw):
                    _, mask, _, length = _HEADER.unpack_from(raw, offset)
                    offset += _HEADER.size
                    name = raw[offset : offset + length].partition(b"\0")[0]
                    offset += length
                    if name in self.names or mask & (
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


def open_wire_watcher(root: Path, names: frozenset[bytes] = _AUTHORITY_FILES,
                      *, modified: bool = False) -> WireWatch:
    """Use Linux notifications when available; callers retain periodic polling.

    ``names`` are the store files in ``root`` that prompt a read; ``modified``
    also reports writes to files that stay open (SQLite's write-ahead log).
    """
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
    if add(fd, os.fsencode(root), _MASK | (_IN_MODIFY if modified else 0)) < 0:
        os.close(fd)
        return PollingWireWatch()
    try:
        return WireChangeWatch(fd, asyncio.get_running_loop(), names)
    except (NotImplementedError, OSError):
        os.close(fd)
        return PollingWireWatch()
