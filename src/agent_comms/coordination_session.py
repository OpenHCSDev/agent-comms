"""Coordination session: owned coordinator state and transitions."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Self

from agent_comms.bus import StoredBusReader
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import (
    CoordinationBeginUnavailable,
    CoordinationReadUnavailable,
    IntegrityViolationError,
)
from agent_comms.diagnostics import PublicationMeasurements
from agent_comms.locked_store import RegistryFile
from agent_comms.store_files import (
    DEFAULT_STORE_CONTENTION, StoreContention,
)

if TYPE_CHECKING:
    from .message_bus import MessageBus
    from .registry_store import RegistryCut
    from .registry_document import RegistrySnapshot
    from .private_registry_guard import RegistryPrivacy


def system_clock() -> int:
    """The coordination session's default millisecond clock."""
    return time.time_ns() // 1_000_000


class NativeCapture(ExitStack, StoredBusReader):
    """Copy a native decision's registry cut, then release it before decoding."""

    def __init__(self, bus: MessageBus) -> None:
        super().__init__()
        self.bus = bus
        self.measurements = PublicationMeasurements()

    @property
    def path(self) -> Path:
        return self.bus.bus.path

    @property
    def registry(self) -> RegistrySnapshot:
        return self.registry_cut.snapshot

    @property
    def privacy(self) -> RegistryPrivacy:
        return self.registry_cut.privacy

    def __enter__(self) -> Self:
        try:
            self.activity = self.activity_clocks()
            with self.measurements.operation("registry_snapshot"):
                self.registry_cut = self.enter_context(self.registry_capture())
            return self
        except BaseException:
            self.close()
            raise

    def registry_capture(self) -> AbstractContextManager[RegistryCut]:
        return nullcontext(self.bus._registry.store.snapshot_with_authority())


class NativeCommit(NativeCapture):
    """Keep a current decoded cut through a bounded write decision."""

    def registry_capture(self) -> AbstractContextManager[RegistryCut]:
        return self.bus._registry.store.current_snapshot()


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

    def capture(self, bus: MessageBus) -> NativeCapture:
        """Return a detached read, never a publication permission."""
        return self._boundary(bus, NativeCapture)

    def _boundary[Boundary: NativeCapture](
        self, bus: MessageBus, boundary_type: type[Boundary],
    ) -> Boundary:
        if bus.bus.path.parent.resolve() != self.path.parent.resolve():
            raise IntegrityViolationError("native boundary belongs to another coordinator")
        return boundary_type(bus)

    @classmethod
    def _begin_transaction(cls, connection: sqlite3.Connection) -> None:
        """Retire existing rollback-journal readers before changing any state."""
        if connection.in_transaction:
            raise IntegrityViolationError("nested coordinator transaction is not allowed")
        with CoordinationBeginUnavailable.checking():
            connection.execute("BEGIN EXCLUSIVE")

    @contextmanager
    def settlement(
        self, boundary: NativeCapture | None = None
    ) -> Iterator[None]:
        """Standalone settlement owns SQL only, never native file resources."""
        with self.writing():
            yield None

    def release_resources(self, snapshot, outcome, boundary: NativeCapture) -> None:
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

    def committing(self, bus: MessageBus) -> NativeCommit:
        return self._boundary(bus, NativeCommit)

    @contextmanager
    def settlement(
        self, boundary: NativeCapture | None = None
    ) -> Iterator[NativeCapture]:
        """Acquire a native boundary, or borrow the caller's already held one."""
        with self.committing(self.bus) if boundary is None else nullcontext(boundary) as acquired:
            if acquired.bus.bus.path.parent.resolve() != self.path.parent.resolve():
                raise IntegrityViolationError("settlement boundary belongs to another coordinator")
            with acquired.registry_cut.current(), self.writing():
                yield acquired

    def release_resources(self, snapshot, outcome, boundary: NativeCapture) -> None:
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
