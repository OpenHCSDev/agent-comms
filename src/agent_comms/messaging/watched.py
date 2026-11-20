"""Async native change delivery for the bus and its consumers."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from agent_comms.storage.locked_store import WireWakeFile
from agent_comms.storage.native_changes import (
    ChangedSources,
    ChangeSource,
    DirectorySource,
    FileLifetimeSource,
    FileSource,
)


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
        self.observed_paths: frozenset[Path] = frozenset()
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
        except (OSError, ValueError) as error:
            # Callback failures must reach the waiting reader, not just stderr.
            self.loop.remove_reader(self.fd)
            self._publish(SourceFailed(error))

    def prepare(self) -> None:
        self._drain()
        self.observed_paths = self._take_changes().declared
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

    def commit_sources(self) -> tuple[FileSource, ...]:
        """This store's own commit signal, without every other store in its root."""
        return tuple(FileSource(path) for path in self.source_paths(self.path.parent))

    def wake_sources(self) -> tuple[FileSource, ...]:
        """The files this store's commits wake its watchers through."""
        return tuple(FileSource(path) for path in WireWakeFile.watched_paths_in(self.path.parent))

    def watch(self, *sources: ChangeSource) -> AsyncGenerator[ChangeSignal]:
        return self.watch_sources(*self.wake_sources(), *sources)

    def changes(self, *sources: ChangeSource) -> ChangedSources:
        """The synchronous registration of the same sources watch follows."""
        return self.register_sources(*self.wake_sources(), *sources)
