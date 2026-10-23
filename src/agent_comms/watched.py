"""Declaration-owned native change registration for the bus and its consumers."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import select
import struct
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import AsyncGenerator, AsyncIterator, Iterable
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from .locked_store import WireWakeFile

# The Linux notification ABI is decoded here, once, not in store consumers.
_MODIFY = 0x00000002
_ATTRIB = 0x00000004
_CLOSE_WRITE = 0x00000008
_MOVED_FROM = 0x00000040
_MOVED_TO = 0x00000080
_CREATE = 0x00000100
_DELETE = 0x00000200
_DELETE_SELF = 0x00000400
_MOVE_SELF = 0x00000800
_OVERFLOW = 0x00004000
_IGNORED = 0x00008000
_MASK = (
    _MODIFY
    | _ATTRIB
    | _CLOSE_WRITE
    | _MOVED_FROM
    | _MOVED_TO
    | _CREATE
    | _DELETE
    | _DELETE_SELF
    | _MOVE_SELF
)
_HEADER = struct.Struct("=iIII")


@dataclass(frozen=True)
class ChangeSource(ABC):
    path: Path

    @property
    @abstractmethod
    def directory(self) -> Path: ...

    @abstractmethod
    def includes(self, path: Path) -> bool: ...

    def observed_directory(self) -> Path:
        directory = self.directory
        while not directory.is_dir():
            directory = directory.parent
        return directory


class FileSource(ChangeSource):
    @property
    def directory(self) -> Path:
        return self.path.parent

    def includes(self, path: Path) -> bool:
        return path == self.path


class DirectorySource(ChangeSource):
    @property
    def directory(self) -> Path:
        return self.path

    def includes(self, path: Path) -> bool:
        return path == self.path or path.parent == self.path


class ChangedSources(ExitStack):
    """One native registration and bounded dirty declared sources.

    Exact and ancestor indexes make an ordinary event independent of the number
    of registrations. Directory sources dirty their declaration, not every new
    allocated filename. Queue loss invalidates every registered source explicitly.
    """

    def __init__(self, *sources: ChangeSource):
        super().__init__()
        self.sources = tuple(dict.fromkeys(sources))
        self._by_path: dict[Path, list[ChangeSource]] = defaultdict(list)
        self._below: dict[Path, set[Path]] = defaultdict(set)
        self._directory_ancestors: set[Path] = set()
        self._dirty: set[Path] = set()
        self._directories: dict[int, Path] = {}
        for source in self.sources:
            self._by_path[source.path].append(source)
            for ancestor in (source.path, *source.path.parents):
                self._below[ancestor].add(source.path)
            self._directory_ancestors.update((source.directory, *source.directory.parents))
        self._libc = ctypes.CDLL(None, use_errno=True)
        init = self._libc.inotify_init1
        init.argtypes, init.restype = [ctypes.c_int], ctypes.c_int
        self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc.inotify_add_watch.restype = ctypes.c_int
        self._libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        self._libc.inotify_rm_watch.restype = ctypes.c_int
        self.fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.callback(os.close, self.fd)
        self._readiness = select.poll()
        self._readiness.register(self.fd, select.POLLIN)
        try:
            self._refresh()
        except BaseException:
            self.close()
            raise

    def _refresh(self, *, lost: bool = False) -> None:
        desired = {source.observed_directory() for source in self.sources}
        for watch, directory in tuple(self._directories.items()):
            if lost or directory not in desired:
                removed = self._libc.inotify_rm_watch(self.fd, watch)
                if removed < 0 and ctypes.get_errno() != errno.EINVAL:
                    raise OSError(ctypes.get_errno(), f"inotify_rm_watch failed for {directory}")
                # EINVAL means the kernel already removed this exact registration.
                del self._directories[watch]
        for directory in desired - set(self._directories.values()):
            watch = self._libc.inotify_add_watch(self.fd, os.fsencode(directory), _MASK)
            if watch < 0:
                raise OSError(ctypes.get_errno(), f"inotify_add_watch failed for {directory}")
            self._directories[watch] = directory

    def _record(self, paths: Iterable[Path]) -> None:
        fresh = set(paths) - self._dirty
        self._dirty.update(fresh)
        if fresh:
            self.changed()

    def invalidate(self) -> None:
        """Lost event coverage requires a fresh read of every declared source."""
        self._record(source.path for source in self.sources)

    def _touch(self, path: Path) -> None:
        self._record(
            source.path
            for key in (path, path.parent)
            for source in self._by_path.get(key, ())
            if source.includes(path)
        )

    def changed(self) -> None:
        """Synchronous readers consume changed_paths; the async capability wakes."""

    def _drain(self) -> None:
        try:
            raw = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        offset = 0
        while offset < len(raw):
            if offset + _HEADER.size > len(raw):
                raise ValueError("Truncated native change header")
            watch, mask, _cookie, length = _HEADER.unpack_from(raw, offset)
            offset += _HEADER.size
            end = offset + length
            if end > len(raw):
                raise ValueError("Truncated native change name")
            name = raw[offset:end].partition(b"\0")[0]
            offset = end
            if mask & _OVERFLOW:
                self.invalidate()
                self._refresh(lost=True)
                continue
            if watch not in self._directories:
                # A retired descriptor no longer identifies a current source.
                self.invalidate()
                continue
            path = self._directories[watch] / os.fsdecode(name)
            self._touch(path)
            if mask & (_DELETE | _MOVED_FROM | _IGNORED | _DELETE_SELF | _MOVE_SELF):
                self._record(self._below.get(path, ()))
            if path in self._directory_ancestors:
                self._record(self._below.get(path, ()))
                self._refresh(lost=bool(mask & (_IGNORED | _DELETE_SELF | _MOVE_SELF)))

    def _take_paths(self) -> frozenset[Path]:
        changed = frozenset(self._dirty)
        self._dirty.clear()
        return changed

    def changed_paths(self) -> frozenset[Path]:
        self._drain()
        if self._readiness.poll(0):
            # A synchronous reader cannot claim complete coverage of a bounded
            # batch while the kernel still holds events. Re-read registered sources
            # rather than keeping stale cached data or draining forever.
            self.invalidate()
        return self._take_paths()


class SourceChange(ABC):
    @abstractmethod
    def accept(self) -> None: ...

    @abstractmethod
    def superseded_by(self, following: SourceChange) -> SourceChange: ...


class SourcesChanged(SourceChange):
    def accept(self) -> None:
        pass

    def superseded_by(self, following: SourceChange) -> SourceChange:
        return following


@dataclass(frozen=True)
class SourceFailed(SourceChange):
    error: Exception

    def accept(self) -> None:
        raise self.error

    def superseded_by(self, following: SourceChange) -> SourceChange:
        return self


class ChangeSignal(ChangedSources, asyncio.Queue[SourceChange]):
    """A bounded native wake/error stream, not an observation/fallback family."""

    def __init__(self, loop: asyncio.AbstractEventLoop, *sources: ChangeSource):
        asyncio.Queue.__init__(self, maxsize=1)
        self.loop = loop
        super().__init__(*sources)

    def _publish(self, change: SourceChange) -> None:
        try:
            previous = self.get_nowait()
        except asyncio.QueueEmpty:
            pass
        else:
            self.task_done()
            change = previous.superseded_by(change)
        self.put_nowait(change)

    def changed(self) -> None:
        self.wake()

    def wake(self) -> None:
        self._publish(SourcesChanged())

    def _on_ready(self) -> None:
        try:
            self._drain()
        except Exception as error:
            # Callback failures must reach the waiting reader, not just stderr.
            self.loop.remove_reader(self.fd)
            self._publish(SourceFailed(error))

    def prepare(self) -> None:
        self._drain()
        self._take_paths()
        try:
            change = self.get_nowait()
        except asyncio.QueueEmpty:
            return
        self.task_done()
        change.accept()

    async def wait(self) -> None:
        change = await self.get()
        self.task_done()
        change.accept()

    @classmethod
    @asynccontextmanager
    async def opened(cls, *sources: ChangeSource) -> AsyncIterator[ChangeSignal]:
        loop = asyncio.get_running_loop()
        opening = asyncio.create_task(asyncio.to_thread(cls, loop, *sources))
        try:
            changes = await asyncio.shield(opening)
        except asyncio.CancelledError:
            changes = await opening
            await asyncio.to_thread(changes.close)
            raise
        loop.add_reader(changes.fd, changes._on_ready)
        try:
            yield changes
        finally:
            loop.remove_reader(changes.fd)
            await asyncio.to_thread(changes.close)


class Watched(WireWakeFile, ABC):
    """A store supplies commit sources through its existing declared wake family."""

    @property
    @abstractmethod
    def path(self) -> Path: ...

    @staticmethod
    def register_sources(*sources: ChangeSource) -> ChangedSources:
        return ChangedSources(*sources)

    @staticmethod
    async def watch_sources(*sources: ChangeSource) -> AsyncGenerator[ChangeSignal]:
        async with ChangeSignal.opened(*sources) as changes:
            while True:
                changes.prepare()
                yield changes
                await changes.wait()

    def watch(self, *sources: ChangeSource) -> AsyncGenerator[ChangeSignal]:
        return self.watch_sources(
            *(FileSource(path) for path in WireWakeFile.watched_paths_in(self.path.parent)),
            *sources,
        )
