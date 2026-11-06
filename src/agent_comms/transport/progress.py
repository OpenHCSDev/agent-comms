"""What a call reports while it runs: how far an export got, and whether its owner cancelled
it. A remote call streams this back; nothing here reads the bus."""

from __future__ import annotations

import asyncio
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from threading import Event


class InterruptibleRead(ABC):
    """A read its observer may interrupt while it runs: a cancelled export stops its scan."""

    @abstractmethod
    def interruptible(self, stopped: Callable[[], bool]) -> AbstractContextManager[None]:
        """While open, the read checks `stopped` and stops when it answers true."""


@dataclass(frozen=True)
class WireExportProgress:
    bytes_written: int
    messages_written: int


class WireExportCancelled(InterruptedError):
    """The owner cancelled this export; staging must unwind without publication."""


class WireExportObserver:
    """Stateless default for CLI and retained-context exports."""

    def advanced(self, progress: WireExportProgress) -> None:
        pass

    def check(self) -> None:
        pass

    async def stopped(self) -> None:
        """An observer that cannot be cancelled never stops a running call."""
        await asyncio.Event().wait()

    @contextmanager
    def reading(self, source: InterruptibleRead) -> Iterator[None]:
        yield


class CancellableWireExport(WireExportObserver):
    def __init__(self) -> None:
        self.stop_requested = Event()
        self.stop_waiters: list[Callable[[], None]] = []
        super().__init__()

    def cancel(self) -> None:
        self.stop_requested.set()
        for wake in tuple(self.stop_waiters):
            wake()

    async def stopped(self) -> None:
        """Resolves when cancel is called, from any thread; no polling."""
        loop, here = asyncio.get_running_loop(), asyncio.Event()

        def wake() -> None:
            loop.call_soon_threadsafe(here.set)

        self.stop_waiters.append(wake)
        try:
            if not self.cancelled():
                await here.wait()
        finally:
            self.stop_waiters.remove(wake)

    def cancelled(self) -> bool:
        return self.stop_requested.is_set()

    def check(self) -> None:
        if self.cancelled():
            raise WireExportCancelled("Wire export cancelled")

    @contextmanager
    def reading(self, source: InterruptibleRead) -> Iterator[None]:
        self.check()
        with source.interruptible(self.cancelled):
            try:
                yield
            except sqlite3.OperationalError:
                self.check()
                raise
