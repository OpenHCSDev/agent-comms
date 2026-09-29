"""Composition and explicit bootstrap of the private coordination owners."""

from __future__ import annotations

from collections.abc import Callable
from typing import Self

from .assignment_store import AssignmentStore
from .attempt_store import AttemptStore
from .coordination_session import CoordinationSession
from .execution_store import ExecutionStore
from .participant_store import ParticipantStore
from .private_runtime_schema import PrivateRuntimeSchema
from .recovery_reader import RecoveryReader


class Coordination:
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
