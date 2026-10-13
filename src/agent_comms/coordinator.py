"""Composition and explicit bootstrap of the private coordination owners."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Self, TypeVar

from .assignment_store import AssignmentStore
from .attempt_store import AttemptStore
from .child_process import join_retirement
from .coordination_session import CoordinationSession
from .execution_store import ExecutionStore
from .participant_store import ParticipantStore
from .private_runtime_schema import PrivateRuntimeSchema
from .recovery_reader import RecoveryReader
from .working_memory_annotations import WorkingMemoryAnnotations

Result = TypeVar("Result")


class Coordination:
    @staticmethod
    async def run_worker(operation: Callable[[], Result]) -> Result:
        """Join an owned blocking operation before its enclosing custody exits.

        The callback owns and closes every borrowed resource it opens. This
        scheduling seam also supports registry-only acquisition/retirement;
        those operations must not depend on opening a coordinator connection.
        """
        pending = asyncio.get_running_loop().run_in_executor(None, operation)
        return await join_retirement(pending)

    @classmethod
    async def run_async(
        cls, path: str | Path, operation: Callable[[Self], Result], *,
        clock_ms: Callable[[], int] | None = None, lock_timeout: float = 5.0,
    ) -> Result:
        """Own a complete operation's connection in its resource worker.

        Only the result crosses back to the event loop. The callback must not
        export this connection or any resource borrowed from it. Cancellation
        joins the acquired operation through connection close; it cannot leave
        a later projection write after the caller releases its custody.
        """
        return await cls.run_worker(
            partial(cls._run_owned, path, operation, clock_ms, lock_timeout)
        )

    @classmethod
    def _run_owned(
        cls, path: str | Path, operation: Callable[[Self], Result],
        clock_ms: Callable[[], int] | None, lock_timeout: float,
    ) -> Result:
        with cls(str(path), clock_ms=clock_ms, lock_timeout=lock_timeout) as store:
            return operation(store)

    def __init__(
        self, path: str, *, clock_ms: Callable[[], int] | None = None, lock_timeout: float = 5.0
    ):
        self.session = CoordinationSession(path, clock_ms=clock_ms, lock_timeout=lock_timeout)
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
