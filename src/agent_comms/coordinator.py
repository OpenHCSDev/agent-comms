"""Composition and explicit bootstrap of the private coordination owners."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import ExitStack, asynccontextmanager
from contextvars import ContextVar
from functools import partial
from pathlib import Path
from queue import SimpleQueue
from threading import Thread
from typing import Self, TypeVar

from .assignment_store import AssignmentStore
from .attempt_store import AttemptStore
from .child_process import join_retirement
from .coordination_database import DEFAULT_STORE_CONTENTION, StoreContention
from .coordination_session import (
    CoordinationSession,
    NativeCoordinationSession,
    ReadNativeCoordinationSession,
)
from .execution_store import ExecutionStore
from .participant_store import ParticipantStore
from .private_runtime_schema import PrivateRuntimeSchema
from .recovery_reader import RecoveryReader
from .store_files import StoreLock
from .working_memory_annotations import WorkingMemoryAnnotations

Result = TypeVar("Result")


class Coordination:
    session_type = NativeCoordinationSession
    _worker: ContextVar[CoordinationWorker | None] = ContextVar("coordination_worker", default=None)

    @classmethod
    @asynccontextmanager
    async def worker(cls, path: str | Path):
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
    async def run_async(
        cls, path: str | Path, operation: Callable[[Self], Result], *,
        clock_ms: Callable[[], int] | None = None,
        contention: StoreContention = DEFAULT_STORE_CONTENTION,
    ) -> Result:
        """Join an operation before releasing its caller's resources.

        A resident worker owns the connection; other callers own it for this
        operation. Neither may export the connection or its borrowed resources.
        """
        if worker := cls._worker.get():
            worker.require(cls, path, clock_ms, contention)
            return await worker.run(operation)
        return await cls.run_worker(
            partial(cls._run_owned, path, operation, clock_ms, contention)
        )

    @classmethod
    def _run_owned(
        cls, path: str | Path, operation: Callable[[Self], Result],
        clock_ms: Callable[[], int] | None, contention: StoreContention,
    ) -> Result:
        with cls(str(path), clock_ms=clock_ms, contention=contention) as store:
            return operation(store)

    @classmethod
    async def run_read_async(
        cls, path: str | Path, operation: Callable[[Self], Result], *,
        clock_ms: Callable[[], int] | None = None,
        contention: StoreContention = DEFAULT_STORE_CONTENTION,
    ) -> Result:
        """Run a typed coordination read; the callback bounds its snapshot."""
        if worker := cls._worker.get():
            worker.require(cls, path, clock_ms, contention)
            return await worker.run_read(operation)
        return await cls.run_worker(
            partial(cls._run_read_owned, path, operation, clock_ms, contention)
        )

    @classmethod
    def _run_read_owned(
        cls, path: str | Path, operation: Callable[[Self], Result],
        clock_ms: Callable[[], int] | None, contention: StoreContention,
    ) -> Result:
        with cls(
            str(path), clock_ms=clock_ms, contention=contention,
            session_type=ReadNativeCoordinationSession,
        ) as store:
            return operation(store)

    def __init__(
        self, path: str, *, clock_ms: Callable[[], int] | None = None,
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
        from .native_runtime_input import NativeRuntimeSchemaMeta  # noqa: F401
        from .typed_table import TypedTable

        for schema in TypedTable.members_with(PrivateRuntimeSchema):
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


class CoordinationWorker:
    """One resident thread owns one writer and one reader, not a connection pool.

    The context binding routes existing async consumers to their worker. It is
    not keyed by path and never stores a connection outside its owning thread.
    Registry-only work still runs independently through Coordination.run_worker.
    """

    def __init__(self, store_type: type[Coordination], path: str | Path):
        self.store_type, self.path = store_type, Path(path).absolute()
        self._operations = SimpleQueue()
        self._ready: Future = Future()
        self._retired: Future = Future()
        self._closed = False
        self._thread = Thread(target=self._serve, name="coordination-store")

    def require(self, store_type, path, clock_ms, contention) -> None:
        # A worker has exactly one store identity and policy. Never silently
        # borrow it for another root, session capability, clock or busy policy.
        if (store_type is not self.store_type or Path(path).absolute() != self.path
                or clock_ms is not None or contention != DEFAULT_STORE_CONTENTION):
            raise ValueError("operation differs from the resident coordination owner")

    def _serve(self) -> None:
        try:
            with ExitStack() as resources:
                writer = resources.enter_context(self.store_type(str(self.path)))
                reader = resources.enter_context(self.store_type(
                    str(self.path), session_type=ReadNativeCoordinationSession,
                ))
                self._ready.set_result(None)
                while (work := self._operations.get()) is not None:
                    future, context, operation, read = work
                    store = reader if read else writer
                    try:
                        # The old close rolled back any unfinished transaction,
                        # even if a callback returned normally. Keep that promise
                        # without discarding the configured connection.
                        try:
                            result = context.run(operation, store)
                        finally:
                            db = store.session._connection
                            if db.in_transaction:
                                db.execute("ROLLBACK")
                    except BaseException as error:
                        future.set_exception(error)
                    else:
                        future.set_result(result)
        except BaseException as error:
            if not self._ready.done():
                self._ready.set_exception(error)
            self._retired.set_exception(error)
        else:
            self._retired.set_result(None)

    async def _run(self, operation, *, read):
        if self._closed:
            raise RuntimeError("coordination worker has retired")
        future: Future = Future()
        self._operations.put((future, StoreLock.worker_context(), operation, read))
        return await join_retirement(asyncio.wrap_future(future))

    async def run(self, operation):
        return await self._run(operation, read=False)

    async def run_read(self, operation):
        return await self._run(operation, read=True)

    async def __aenter__(self):
        self._thread.start()
        try:
            await join_retirement(asyncio.wrap_future(self._ready))
        except BaseException:
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *_exc):
        self._closed = True
        self._operations.put(None)
        try:
            await join_retirement(asyncio.wrap_future(self._retired))
        finally:
            # SQLite and all queued operations have retired before the thread
            # exits; joining cannot leave connection cleanup behind its owner.
            self._thread.join()
