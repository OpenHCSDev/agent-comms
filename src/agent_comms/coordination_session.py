"""Coordination session: owned coordinator state and transitions."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Self

from agent_comms.bus import StoredBusReader
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import (
    CoordinationBeginUnavailable,
    CoordinationReadUnavailable,
    IntegrityViolationError,
    StaleFence,
)
from agent_comms.diagnostics import PublicationMeasurements
from agent_comms.locked_store import RegistryFile
from agent_comms.store_files import (
    DEFAULT_STORE_CONTENTION, StoreContention,
)

if TYPE_CHECKING:
    from .message_bus import MessageBus


def system_clock() -> int:
    """The coordination session's default millisecond clock."""
    return time.time_ns() // 1_000_000


class CoordinationSession(CoordinationStore):
    def __init__(
        self, path: str, *, clock_ms: Callable[[], int] = system_clock,
        contention: StoreContention = DEFAULT_STORE_CONTENTION,
    ) -> None:
        super().__init__(path, contention)
        self._clock_ms = clock_ms

    def now(self, floor: int = 0) -> int:
        now = self._clock_ms()
        if type(now) is not int or now < 0:
            raise ValueError("coordinator clock must return a nonnegative integer millisecond")
        return max(now, floor)

    @classmethod
    def _begin_transaction(cls, connection: sqlite3.Connection) -> None:
        """Retire existing rollback-journal readers before changing any state."""
        if connection.in_transaction:
            raise IntegrityViolationError("nested coordinator transaction is not allowed")
        with CoordinationBeginUnavailable.checking():
            connection.execute("BEGIN EXCLUSIVE")

    @contextmanager
    def settlement(
        self, boundary: NativeBoundary | None = None
    ) -> Iterator[None]:
        """Standalone settlement owns SQL only, never native file resources."""
        with self.writing():
            yield None

    def release_resources(self, snapshot, outcome, boundary: NativeBoundary) -> None:
        """Standalone attempts cannot acquire native file claims."""

    @contextmanager
    def read(self) -> Iterator[None]:
        """Project one committed database state, or reuse an enclosing write transaction."""
        db = self._connection
        if db.in_transaction:
            yield
            return
        with CoordinationReadUnavailable.checking():
            try:
                self.begin_snapshot(db)
                yield
            finally:
                if db.in_transaction:
                    db.execute("ROLLBACK")


class NativeBoundary(ExitStack, StoredBusReader):
    """One native decision's bus cut and registry revision.

    The registry guard is held only to copy the revision, never for the
    decision's lifetime: a write made from the copy runs under ``current``.
    """

    def __init__(self, bus: MessageBus) -> None:
        super().__init__()
        self.bus = bus
        self.measurements = PublicationMeasurements()

    @property
    def path(self) -> Path:
        return self.bus.bus.path

    def __enter__(self) -> Self:
        try:
            self.activity = self.activity_clocks()
            store = self.bus._registry.store
            with (self.measurements.operation("file_exclusion"), self.read_guard(store.path),
                  self.measurements.operation("registry_snapshot")):
                self.privacy = store.private_guard_unlocked()
                self.registry = store.read_snapshot_unlocked()
                self.registry_revision = store.revision_unlocked()
            return self
        except BaseException:
            self.close()
            raise

    @contextmanager
    def current(self) -> Iterator[None]:
        """Exclude registry writers for one write made from this decision's copy.

        A registry write after the copy refuses the write, naming the change.
        """
        store = self.bus._registry.store
        with self.read_guard(store.path):
            if store.revision_unlocked() != self.registry_revision:
                raise StaleFence(
                    "native registry revision changed after this decision read it")
            yield


class NativeCoordinationSession(CoordinationSession):
    """Native sessions declare their bus boundary, independent of file existence."""

    @cached_property
    def bus(self) -> MessageBus:
        """Create native bus owners only when this session first needs a boundary."""
        from . import message_bus, publisher, registration

        return message_bus.MessageBus(
            self.path.parent,
            registration.Registration(RegistryFile.path_in(self.path.parent)),
            publisher.ClaimWrites,
        )

    def boundary(self, bus: MessageBus) -> NativeBoundary:
        if bus.bus.path.parent.resolve() != self.path.parent.resolve():
            raise IntegrityViolationError("native boundary belongs to another coordinator")
        return NativeBoundary(bus)

    @contextmanager
    def settlement(
        self, boundary: NativeBoundary | None = None
    ) -> Iterator[NativeBoundary]:
        """Acquire a native boundary, or borrow the caller's already held one."""
        with self.boundary(self.bus) if boundary is None else nullcontext(boundary) as acquired:
            if acquired.bus.bus.path.parent.resolve() != self.path.parent.resolve():
                raise IntegrityViolationError("settlement boundary belongs to another coordinator")
            with acquired.current(), self.writing():
                yield acquired

    def release_resources(self, snapshot, outcome, boundary: NativeBoundary) -> None:
        snapshot.release_resources(boundary, outcome)


class ReadNativeCoordinationSession(NativeCoordinationSession):
    """Read-only native coordination through the complete session constructor."""

    def _prepare_private_file(self) -> None:
        self._private_file()

    def _open_connection(self, contention: StoreContention) -> sqlite3.Connection:
        return self._open_reader(self.path, contention)

    def _configure_connection(self) -> None:
        pass  # _open_reader configured query-only behavior before publication.

    def _initialize_schema(self) -> None:
        pass  # Consumers validate exactly the typed tables they read.

    def _enforce_private_modes(self) -> None:
        pass  # A reader observes private storage and never repairs it.
