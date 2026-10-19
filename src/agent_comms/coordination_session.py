"""Coordination session: owned coordinator state and transitions."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from functools import cached_property
from typing import TYPE_CHECKING

from agent_comms.coordination_database import (
    DEFAULT_STORE_CONTENTION,
    CoordinationStore,
    StoreContention,
)
from agent_comms.coordination_errors import (
    CoordinationReadUnavailable,
    IntegrityViolationError,
)

if TYPE_CHECKING:
    from .message_bus import MessageBus


class CoordinationSession(CoordinationStore):
    def __init__(
        self, path: str, *, clock_ms: Callable[[], int] | None = None,
        contention: StoreContention = DEFAULT_STORE_CONTENTION,
    ) -> None:
        super().__init__(path, contention)
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def now(self, floor: int = 0) -> int:
        now = self._clock_ms()
        if type(now) is not int or now < 0:
            raise ValueError("coordinator clock must return a nonnegative integer millisecond")
        return max(now, floor)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Allow a rollback-journal writer only after existing readers retire.

        ``BEGIN IMMEDIATE`` lets new work proceed before old readers release,
        then makes those readers a fallible dependency of ``COMMIT``.  Every
        coordinator write instead acquires the exclusive rollback-journal lock
        before changing state, so contention is handled before writing and
        a successfully authorized transaction owns its complete commit lifetime.
        """
        with self._transaction_lifetime() as db:
            db.execute("BEGIN EXCLUSIVE")
            yield db

    @contextmanager
    def settlement(
        self, boundary: NativeBoundary | None = None
    ) -> Iterator[None]:
        """Standalone settlement owns SQL only, never native file resources."""
        with self.transaction():
            yield None

    def release_resources(self, snapshot, outcome, boundary: NativeBoundary | None) -> None:
        """Standalone attempts cannot acquire native file claims."""

    @contextmanager
    def _transaction_lifetime(self) -> Iterator[sqlite3.Connection]:
        """Shared non-nesting, commit and rollback ownership for write scopes."""
        db = self._connection
        if db.in_transaction:
            raise IntegrityViolationError("nested coordinator transaction is not allowed")
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise

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


class NativeBoundary(ExitStack):
    """Publication locks ordered by their namespace, log and registry owners."""

    def __init__(self, bus, *, blocking=True, contention=None, measurements=None):
        super().__init__()
        from .diagnostics import PublicationMeasurements

        self.bus, self.blocking = bus, blocking
        self.contention = contention
        self.measurements = measurements if measurements is not None else PublicationMeasurements()

    def __enter__(self):
        from .store_files import ExclusiveLock, SharedLock, StoreLock

        try:
            self.activity = self.enter_context(self.bus.opened_activity())
            with self.measurements.operation("file_exclusion"):
                locks = self.enter_context(StoreLock.ordered(
                    {self.bus: SharedLock(), self.bus.log: ExclusiveLock(),
                     self.bus._registry.store: SharedLock()},
                    blocking=self.blocking, contention=self.contention))
            self.bus_lock = locks[self.bus.log]
            with self.measurements.operation("registry_snapshot"):
                self.registry = self.bus._registry.store._read_unlocked().snapshot()
            return self
        except BaseException:
            self.close()
            raise


class NativeCoordinationSession(CoordinationSession):
    """Native sessions declare their bus boundary, independent of file existence."""

    @cached_property
    def bus(self) -> MessageBus:
        """Create native bus owners only when this session first needs a boundary."""
        from . import message_bus, publisher, registration

        return message_bus.MessageBus(
            self.path.parent / "bus.jsonl",
            registration.Registration(self.path.parent / "registry.json"),
            publisher.ClaimWrites,
        )

    def boundary(self, bus=None, **options):
        selected = self.bus if bus is None else bus
        if selected.log.path.parent.resolve() != self.path.parent.resolve():
            raise IntegrityViolationError("native boundary belongs to another coordinator")
        return NativeBoundary(selected, **options)

    @contextmanager
    def settlement(
        self, boundary: NativeBoundary | None = None
    ) -> Iterator[NativeBoundary]:
        """Acquire a native boundary, or borrow the caller's already held one."""
        with self.boundary() if boundary is None else nullcontext(boundary) as acquired:
            if acquired.bus.log.path.parent.resolve() != self.path.parent.resolve():
                raise IntegrityViolationError("settlement boundary belongs to another coordinator")
            with self.transaction():
                yield acquired

    def release_resources(self, snapshot, outcome, boundary: NativeBoundary | None) -> None:
        if not isinstance(boundary, NativeBoundary):
            raise IntegrityViolationError("native release requires an acquired boundary")
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
