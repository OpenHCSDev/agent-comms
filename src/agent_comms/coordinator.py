"""Composition and explicit bootstrap of the private coordination owners."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager, AsyncExitStack
from functools import partial
from pathlib import Path
from typing import ParamSpec, Self, TypeVar

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
Parameters = ParamSpec("Parameters")


class Coordination:
    session_type = NativeCoordinationSession

    @staticmethod
    async def run_worker(
        operation: Callable[Parameters, Result],
        *args: Parameters.args, **kwargs: Parameters.kwargs,
    ) -> Result:
        """Wait for blocking work before its caller releases the enclosing resources.

        The callback owns and closes every borrowed resource it opens. This
        scheduling seam also supports registry-only acquisition/retirement;
        those operations must not depend on opening a coordinator connection.
        """
        pending = asyncio.get_running_loop().run_in_executor(
            None, StoreLock.worker_context().run, partial(operation, *args, **kwargs)
        )
        return await join_retirement(pending)

    @staticmethod
    async def enter_context(
        resources: AsyncExitStack, context: AbstractContextManager[Result],
    ) -> Result:
        """Join entry before cancellation, and pass the real exit back to its worker."""
        def enter() -> Result:
            value = context.__enter__()
            resources.push_async_exit(
                lambda *exit_info: Coordination.run_worker(context.__exit__, *exit_info)
            )
            return value

        return await Coordination.run_worker(enter)

    @classmethod
    async def run_async(
        cls, path: str | Path, operation: Callable[[Self], Result], *,
        clock_ms: Callable[[], int] | None = None,
        contention: StoreContention = DEFAULT_STORE_CONTENTION,
    ) -> Result:
        """Own a complete operation's connection in its resource worker.

        Only the result crosses back to the event loop. The callback must not
        export this connection or any resource borrowed from it. Cancellation
        joins the acquired operation through connection close; it cannot leave
        a later derived-data write after the caller releases its resources.
        """
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
        """Run a typed coordination read on the one committed snapshot owner."""
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
