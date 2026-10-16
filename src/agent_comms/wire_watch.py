"""Local file notification for idle wire owners.

A native notification only prompts a normal read. Missing directories are
watched through their existing ancestors; polling is used only when native
notification is unavailable.
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
from typing import ClassVar

from .goal_waits import GoalWaits

_IN_MODIFY = 0x00000002
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_MASK = (
    _IN_CLOSE_WRITE | _IN_MOVED_FROM | _IN_MOVED_TO | _IN_DELETE
    | _IN_DELETE_SELF | _IN_MOVE_SELF
)
_HEADER = struct.Struct("=iIII")
_AUTHORITY_FILES = frozenset({
    b"bus.jsonl",
    b"registry.json",
    b".registry-owner-guard",
    GoalWaits.filename.encode(),
})


@dataclass(frozen=True)
class WatchSource(ABC):
    """Files in one directory whose declared writes wake a reader."""

    directory: Path
    modified: ClassVar[bool] = False

    @abstractmethod
    def includes(self, name: bytes) -> bool: ...

    @classmethod
    def wire(cls, root: Path) -> WatchSource:
        return ReplacedWatchSource(root, _AUTHORITY_FILES)

    @classmethod
    def file(cls, path: Path) -> WatchSource:
        return ReplacedWatchSource(path.parent, frozenset({os.fsencode(path.name)}))

    @classmethod
    def modified_file(cls, path: Path) -> WatchSource:
        return ModifiedWatchSource(path.parent, frozenset({os.fsencode(path.name)}))


@dataclass(frozen=True)
class ReplacedWatchSource(WatchSource):
    """Atomic replacement and directory-entry changes wake this source."""

    names: frozenset[bytes]

    def includes(self, name: bytes) -> bool:
        return name in self.names


class ModifiedWatchSource(ReplacedWatchSource):
    """In-place writes as well as directory-entry changes wake this source."""

    modified = True


class DirectoryWatchSource(WatchSource):
    """Observe entries whose names are allocated by the directory's writer."""

    def includes(self, name: bytes) -> bool:
        return True


@dataclass(frozen=True)
class WatchedDirectory:
    sources: tuple[WatchSource, ...]
    modified: bool
    reopen: frozenset[bytes]

    def includes(self, name: bytes) -> bool:
        return any(source.includes(name) for source in self.sources)


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
    event_settle_seconds = 0.025

    def __init__(
        self, fd: int, loop: asyncio.AbstractEventLoop,
        sources: tuple[WatchSource, ...],
        directories: dict[int, WatchedDirectory],
    ):
        self.sources = sources
        self.directories = directories
        self.fd = fd
        self.loop = loop
        self.changed = asyncio.Event()
        self.invalid = False
        self._pending_change: asyncio.TimerHandle | None = None
        loop.add_reader(fd, self._on_ready)

    async def prepare(self) -> None:
        self.changed.clear()

    async def wait(self) -> WireWatch:
        if self.invalid:
            self.close()
            return open_wire_watcher(*self.sources)
        await self.changed.wait()
        return self

    def notify(self) -> None:
        self.changed.set()

    def _schedule_changed(self) -> None:
        # Settle one bounded burst from its first relevant change. Moving the
        # deadline on every shared-store write can starve an addressed owner
        # indefinitely while other owners keep publishing.
        if self._pending_change is None:
            self._pending_change = self.loop.call_later(
                self.event_settle_seconds, self._publish_changed
            )

    def _publish_changed(self) -> None:
        self._pending_change = None
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
                    directory = self.directories.get(watch)
                    if (directory is not None and directory.includes(name)) or mask & (
                        _IN_Q_OVERFLOW | _IN_IGNORED | _IN_DELETE_SELF | _IN_MOVE_SELF
                    ):
                        self._schedule_changed()
                    if (
                        (directory is not None and name in directory.reopen)
                        or mask & (_IN_IGNORED | _IN_DELETE_SELF | _IN_MOVE_SELF)
                    ):
                        self.invalid = True
        except BlockingIOError:
            pass
        except OSError:
            self.invalid = True
            self.changed.set()

    def close(self) -> None:
        if self._pending_change is not None:
            self._pending_change.cancel()
            self._pending_change = None
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
    libc = ctypes.CDLL(None, use_errno=True)
    init = libc.inotify_init1
    add = libc.inotify_add_watch
    init.argtypes = [ctypes.c_int]
    init.restype = ctypes.c_int
    add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    add.restype = ctypes.c_int
    fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1 failed")
    grouped: dict[Path, WatchedDirectory] = {}
    for source in sources:
        directory = source.directory
        observed, reopen = source, frozenset()
        while not directory.is_dir() and directory != directory.parent:
            reopen = frozenset({os.fsencode(directory.name)})
            directory = directory.parent
            observed = ReplacedWatchSource(directory, reopen)
        current = grouped.get(
            directory, WatchedDirectory((), False, frozenset())
        )
        grouped[directory] = WatchedDirectory(
            (*current.sources, observed),
            current.modified or (source.modified and not reopen),
            current.reopen | reopen,
        )
    directories = {}
    for directory, watched in grouped.items():
        watch = add(
            fd, os.fsencode(directory),
            _MASK
            | (_IN_MODIFY if watched.modified else 0)
            | (_IN_CREATE if watched.reopen else 0),
        )
        if watch < 0:
            error = OSError(ctypes.get_errno(), f"inotify_add_watch failed for {directory}")
            os.close(fd)
            raise error
        directories[watch] = watched
    try:
        return WireChangeWatch(
            fd, asyncio.get_running_loop(), tuple(sources),
            directories,
        )
    except (NotImplementedError, OSError):
        os.close(fd)
        raise
