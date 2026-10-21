"""Composition and explicit bootstrap of the private coordination owners."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack, asynccontextmanager
from contextvars import ContextVar
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self, TypeVar

from .assignment_store import AssignmentStore
from .attempt_store import AttemptStore
from .child_process import join_retirement
from .coordination_database import DEFAULT_STORE_CONTENTION, StoreContention
from .coordination_session import (
    CoordinationSession,
    NativeCoordinationSession,
    ReadNativeCoordinationSession,
    system_clock,
)
from .execution_store import ExecutionStore
from .participant_store import ParticipantStore
from .private_runtime_schema import PrivateRuntimeSchema
from .recovery_reader import RecoveryReader
from .store_files import StoreLock
from .working_memory_annotations import WorkingMemoryAnnotations

if TYPE_CHECKING:
    from .comms import Comms
    from .message_bus import WatchPass

Result = TypeVar("Result")


class CoordinationRoute(ABC):
    """The async store lifetime selected by the caller's context."""

    __slots__ = ()

    @abstractmethod
    async def run(
        self,
        store_type: type[Coordination],
        path: str | Path,
        operation: Callable[..., Result],
        clock_ms: Callable[[], int],
        contention: StoreContention,
    ) -> Result: ...

    @abstractmethod
    async def run_read(
        self,
        store_type: type[Coordination],
        path: str | Path,
        operation: Callable[..., Result],
        clock_ms: Callable[[], int],
        contention: StoreContention,
    ) -> Result: ...


class StandaloneRoute(CoordinationRoute):
    """Callers outside a worker own their stores for one joined operation."""

    __slots__ = ()

    async def run(
        self,
        store_type: type[Coordination],
        path: str | Path,
        operation: Callable[..., Result],
        clock_ms: Callable[[], int],
        contention: StoreContention,
    ) -> Result:
        return await store_type.run_worker(
            partial(store_type._run_owned, path, operation, clock_ms, contention)
        )

    async def run_read(
        self,
        store_type: type[Coordination],
        path: str | Path,
        operation: Callable[..., Result],
        clock_ms: Callable[[], int],
        contention: StoreContention,
    ) -> Result:
        return await store_type.run_worker(
            partial(store_type._run_read_owned, path, operation, clock_ms, contention)
        )


STANDALONE_ROUTE: Final = StandaloneRoute()


class Coordination:
    session_type = NativeCoordinationSession
    _worker: ContextVar[CoordinationRoute] = ContextVar(
        "coordination_worker",
        default=STANDALONE_ROUTE,
    )

    @classmethod
    @asynccontextmanager
    async def worker(cls, path: str | Path) -> AsyncIterator[CoordinationWorker]:
        """Own this process's async stores until every native producer has retired."""
        async with CoordinationWorker(cls, path) as worker:
            token = cls._worker.set(worker)
            try:
                yield worker
            finally:
                cls._worker.reset(token)

    @staticmethod
    async def run_worker(operation: Callable[[], Result]) -> Result:
        """Wait for blocking work before its caller releases the enclosing resources.

        The callback owns and closes every borrowed resource it opens. This
        scheduling seam also supports registry-only acquisition/retirement;
        those operations must not depend on opening a coordinator connection.
        """
        pending = asyncio.get_running_loop().run_in_executor(
            None, StoreLock.worker_context().run, operation
        )
        return await join_retirement(pending)

    @classmethod
    async def watch_worker(
        cls, watch: WatchPass, opened_wire: Callable[[], AbstractContextManager[Comms]],
    ) -> None:
        """Join each entered pass before idling; cancellation also joins borrowed work."""
        while True:
            try:
                await cls.run_worker(partial(watch.watch_pass, opened_wire))
            except Exception:
                logging.exception("Service worker pass failed: %s", type(watch).__name__)
            await asyncio.sleep(60)

    @classmethod
    async def run_async(
        cls, path: str | Path, operation: Callable[[Self], Result], *,
        clock_ms: Callable[[], int] = system_clock,
        contention: StoreContention = DEFAULT_STORE_CONTENTION,
    ) -> Result:
        """Join an operation before releasing its caller's resources.

        A resident worker owns the connection; other callers own it for this
        operation. Neither may export the connection or its borrowed resources.
        """
        return await cls._worker.get().run(cls, path, operation, clock_ms, contention)

    @classmethod
    def _run_owned(
        cls, path: str | Path, operation: Callable[[Self], Result],
        clock_ms: Callable[[], int], contention: StoreContention,
    ) -> Result:
        with cls(str(path), clock_ms=clock_ms, contention=contention) as store:
            return operation(store)

    @classmethod
    async def run_read_async(
        cls, path: str | Path, operation: Callable[[Self], Result], *,
        clock_ms: Callable[[], int] = system_clock,
        contention: StoreContention = DEFAULT_STORE_CONTENTION,
    ) -> Result:
        """Run a typed coordination read; the callback bounds its snapshot."""
        return await cls._worker.get().run_read(cls, path, operation, clock_ms, contention)

    @classmethod
    def _run_read_owned(
        cls, path: str | Path, operation: Callable[[Self], Result],
        clock_ms: Callable[[], int], contention: StoreContention,
    ) -> Result:
        with cls(
            str(path),
            clock_ms=clock_ms,
            contention=contention,
            session_type=ReadNativeCoordinationSession,
        ) as store:
            return operation(store)

    def __init__(
        self, path: str, *, clock_ms: Callable[[], int] = system_clock,
        contention: StoreContention = DEFAULT_STORE_CONTENTION,
        session_type: type[CoordinationSession] | None = None,
    ):
        owner = self.session_type if session_type is None else session_type
        self.session = owner(path, clock_ms=clock_ms, contention=contention)
        self._bind_tables()

    def _bind_tables(self) -> None:
        self.participants = ParticipantStore(self.session)
        self.assignments = AssignmentStore(self.session)
        self.snapshots = RecoveryReader(self.session, self.participants, self.assignments)
        self.executions = ExecutionStore(
            self.session, self.participants, self.assignments, self.snapshots
        )
        self.attempts = AttemptStore(self.session, self.participants, self.snapshots)
        self.annotations = WorkingMemoryAnnotations(self.session)

    def install_private_runtime(self) -> None:
        """Explicit protocol/owner bootstrap, never invoked by a reader."""
        # Load the canonical declarations before querying their existing family.
        from . import cohort_schema, coordination_response, native_prompt_binding  # noqa: F401
        from . import native_runtime_input  # noqa: F401

        for schema in PrivateRuntimeSchema.__subclasses__():
            schema.install(self)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class StandaloneCoordination(Coordination):
    """SQL-only consumers explicitly select the standalone session capability."""

    session_type = CoordinationSession


class CoordinationWorker(CoordinationRoute):
    """One executor thread owns the worker's writer, reader and their retirement.

    The context binding is not keyed by path. Registry-only work still runs
    independently through Coordination.run_worker. The executor carries callback
    errors and owns scheduling/shutdown; join_retirement preserves cancellation.
    """

    def __init__(self, store_type: type[Coordination], path: str | Path):
        self.store_type, self.path = store_type, Path(path).absolute()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            initializer=self._open,
            thread_name_prefix="coordination-store",
        )

    def require(
        self,
        store_type: type[Coordination],
        path: str | Path,
        clock_ms: Callable[[], int],
        contention: StoreContention,
    ) -> None:
        # Match the resident factory, including its default clock/busy policy.
        # Another store identity or policy must never silently borrow it.
        if (store_type, Path(path).absolute(), clock_ms, contention) != (
            self.store_type,
            self.path,
            system_clock,
            DEFAULT_STORE_CONTENTION,
        ):
            raise ValueError("operation differs from the resident coordination owner")

    def _open(self) -> None:
        # A failed initializer closes partial construction on this same thread.
        with ExitStack() as resources:
            self._writer = resources.enter_context(self.store_type(str(self.path)))
            self._reader = resources.enter_context(
                self.store_type(
                    str(self.path),
                    session_type=ReadNativeCoordinationSession,
                )
            )
            self._resources = resources.pop_all()

    @staticmethod
    def _apply(operation: Callable[..., Result], store: Coordination) -> Result:
        try:
            return operation(store)
        finally:
            # Closing the old per-operation connection rolled back unfinished
            # transactions even on normal return. Retain that callback boundary.
            db = store.session._connection
            if db.in_transaction:
                db.execute("ROLLBACK")

    def _write(self, operation: Callable[..., Result]) -> Result:
        return self._apply(operation, self._writer)

    def _read(self, operation: Callable[..., Result]) -> Result:
        return self._apply(operation, self._reader)

    async def _submit(self, operation: Callable[[], Result]) -> Result:
        pending = self._executor.submit(StoreLock.worker_context().run, operation)
        await join_retirement(asyncio.wrap_future(pending))
        return pending.result()

    async def run(
        self,
        store_type: type[Coordination],
        path: str | Path,
        operation: Callable[..., Result],
        clock_ms: Callable[[], int],
        contention: StoreContention,
    ) -> Result:
        self.require(store_type, path, clock_ms, contention)
        return await self._submit(partial(self._write, operation))

    async def run_read(
        self,
        store_type: type[Coordination],
        path: str | Path,
        operation: Callable[..., Result],
        clock_ms: Callable[[], int],
        contention: StoreContention,
    ) -> Result:
        self.require(store_type, path, clock_ms, contention)
        return await self._submit(partial(self._read, operation))

    def _close(self) -> None:
        self._resources.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        try:
            await self._submit(self._close)
        finally:
            self._executor.shutdown(wait=True)
