"""Attempt rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntFlag
from typing import TYPE_CHECKING, Annotated, ClassVar, Final

from agent_comms.attempt_states import AttemptState
from agent_comms.coordination_contracts import (
    MAX_IDENTIFIER_CHARS,
    require_bounded,
    require_nonempty,
    validate_execution_id,
)
from agent_comms.coordination_errors import RecoveryBlocked, StaleFence
from agent_comms.coordination_schema import CoordinatorRow
from agent_comms.coordination_tables.participants import OwnerGenerations, OwnerRecord
from agent_comms.field_codec import SnapshotExclude, projected, ThreadLookup
from agent_comms.owner_fence import AttemptAuthority
from agent_comms.reason_codes import ReasonCode
from agent_comms.typed_table import (
    Column,
    CounterDependent,
    FinalizedLifecycle,
    ForeignKey,
    FrozenColumn,
    Generated,
    GuardedInserts,
    IncrementingColumn,
    LifecycleTransitions,
    MonotoneColumns,
    NoDelete,
    NonDecreasing,
    NonDecreasingWhenPresent,
    TextStorage,
)

if TYPE_CHECKING:
    from agent_comms.coordination_snapshot import RecoverySnapshot


class ReplayFact(IntFlag):
    NONE = 0
    ASSISTANT_OUTPUT = 1 << 0
    THINKING_OUTPUT = 1 << 1
    TOOL_CALL_STARTED = 1 << 2
    TOOL_EXECUTED = 1 << 3
    INPUT_FORWARDED = 1 << 4
    COMPACTION = 1 << 5
    PROJECT_EFFECTS = 1 << 6
    WIRE_EFFECTS = 1 << 7
    EXTENSION_EFFECTS = 1 << 8
    UNKNOWN_EFFECTS = 1 << 9


APPROVED_REPLAY_FACT_MASK: Final = sum(fact.value for fact in ReplayFact)


class PreviousAttempt(ABC):
    """Start policy consumes an attempt capability, not the snapshot's optional SQL row."""

    @abstractmethod
    def require_attempt(self) -> AttemptRecord: ...

    @abstractmethod
    def require_retry(self, snapshot: RecoverySnapshot) -> None: ...

    @abstractmethod
    def require_newer_owner(self, generation: int) -> None: ...


class NoPreviousAttempt(PreviousAttempt):
    def require_newer_owner(self, generation: int) -> None:
        pass

    def require_attempt(self) -> AttemptRecord:
        raise StaleFence("execution has no authorized attempt")

    def require_retry(self, snapshot: RecoverySnapshot) -> None:
        raise RecoveryBlocked("frozen v2 cannot resume an unstarted deferral")


@dataclass(frozen=True, slots=True)
class AttemptRecord(
    LifecycleTransitions,
    FinalizedLifecycle,
    CounterDependent,
    IncrementingColumn,
    MonotoneColumns,
    OwnerRecord,
    GuardedInserts,
    NoDelete,
    CoordinatorRow,
    PreviousAttempt,
    declared_name="attempts",
):
    counter_reference = ForeignKey(
        columns=("owner_lookup", "owner_generation"), target=OwnerGenerations, target_columns=("owner_lookup", "generation")
    )
    column_orders = (
        NonDecreasing(("updated_at_ms", "last_progress_at_ms", "backend_done", "process_dead")),
        NonDecreasingWhenPresent(("lease_expires_at_ms",)),
    )
    insert_condition = """(NOT EXISTS (SELECT 1 FROM executions e
        WHERE e.execution_id = NEW.execution_id AND e.owner_lookup = NEW.owner_lookup
        AND NEW.attempt_ordinal <= e.max_attempts AND ((NEW.attempt_ordinal = 1
        AND (e.status = 'pending' OR (e.status = 'active' AND e.current_attempt_ordinal = 1)))
        OR (NEW.attempt_ordinal > 1 AND (e.status = 'deferred' OR (e.status = 'active'
        AND e.current_attempt_ordinal = NEW.attempt_ordinal))))
        AND NEW.attempt_ordinal = 1 + coalesce((SELECT max(a.attempt_ordinal) FROM attempts a
        WHERE a.execution_id = e.execution_id), 0)))
        OR (NOT EXISTS (SELECT 1 FROM owner_generations g WHERE g.owner_lookup = NEW.owner_lookup
        AND g.generation = NEW.owner_generation))
        OR ((json_extract(NEW.lifecycle, '$.kind')) != 'prompt_starting' OR NEW.revision != 1
        OR (CASE WHEN json_extract(NEW.lifecycle, '$.kind') IN ({terminal_attempt_names})
        THEN 1 ELSE json_extract(NEW.lifecycle, '$.backend_done') END) != 0
        OR (CASE WHEN json_extract(NEW.lifecycle, '$.kind') IN ({terminal_attempt_names})
        THEN 1 ELSE json_extract(NEW.lifecycle, '$.process_dead') END) != 0)
        OR (NEW.attempt_ordinal > 1 AND (NOT EXISTS (SELECT 1 FROM retry_disposition_basis b
        WHERE b.execution_id = NEW.execution_id AND b.authorized = 1)
        OR EXISTS (SELECT 1 FROM current_executions p WHERE p.owner_lookup = NEW.owner_lookup
        AND p.execution_id IS NOT NULL))) OR (NEW.attempt_ordinal > 1 AND NOT EXISTS (
        SELECT 1 FROM attempts a WHERE a.execution_id = NEW.execution_id
        AND a.attempt_ordinal = NEW.attempt_ordinal - 1 AND a.phase IN ({retry_attempt_phases})
        AND a.backend_done = 1 AND a.process_dead = 1 AND NEW.owner_generation > a.owner_generation
        AND NEW.owner_token_digest != a.owner_token_digest))"""

    def require_attempt(self) -> AttemptRecord:
        return self

    def require_retry(self, snapshot: RecoverySnapshot) -> None:
        if not snapshot.can_retry:
            raise RecoveryBlocked("retry requires final death, replay proof, and budget")

    def require_newer_owner(self, generation: int) -> None:
        if generation <= self.owner_generation:
            raise StaleFence("retry must use a strictly newer owner generation")

    def advance(
        self,
        session,
        phase: type[AttemptState],
        *,
        backend_done: bool,
        process_dead: bool,
        progress: bool,
        reason_code: ReasonCode,
    ) -> None:
        """Record a checked phase inside the caller's fenced transaction."""
        db = session._connection
        now = session.now(self.updated_at_ms)
        AttemptRecord.update(
            db,
            where="execution_id=? AND attempt_ordinal=?",
            parameters=(self.execution_id, self.attempt_ordinal),
            lifecycle=self.lifecycle.observed(
                phase, backend_done=backend_done, process_dead=process_dead, progress=progress
            ),
            revision=self.revision + 1,
            updated_at_ms=now,
            last_progress_at_ms=now if progress else self.last_progress_at_ms,
            reason_code=reason_code,
        )

    @property
    def authority(self) -> AttemptAuthority:
        return AttemptAuthority(
            self.execution_id,
            self.attempt_ordinal,
            self.owner_lookup,
            self.owner_generation,
            self.owner_token_digest,
        )

    execution_id: Annotated[str, SnapshotExclude(), FrozenColumn(primary_key=True)]
    attempt_ordinal: Annotated[int, FrozenColumn(primary_key=True, check="attempt_ordinal > 0")]
    owner_lookup: Annotated[ThreadLookup, FrozenColumn()]
    owner_generation: Annotated[int, FrozenColumn(check="owner_generation > 0")]
    owner_token_digest: Annotated[
        str,
        SnapshotExclude(),
        FrozenColumn(unique=True, check="length(owner_token_digest) BETWEEN 1 AND 256"),
    ]
    lifecycle: Annotated[AttemptState, SnapshotExclude()]
    revision: Annotated[int, Column(check="revision > 0")]
    last_progress_at_ms: int | None
    reason_code: Annotated[
        ReasonCode,
        Column(storage=TextStorage, nullable=True,
               check="reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 64"),
    ]
    created_at_ms: Annotated[int, FrozenColumn(check="created_at_ms >= 0")]
    updated_at_ms: Annotated[int, Column(check="updated_at_ms >= created_at_ms")]

    @projected(view="snapshot", name="phase")
    def snapshot_phase(self):
        return type(self.lifecycle)

    @projected(view="snapshot", name="lease_expires_at_ms")
    def snapshot_lease_expires_at_ms(self):
        return self.lifecycle.lease_expires_at_ms

    @projected(view="snapshot", name="backend_done")
    def snapshot_backend_done(self):
        return self.lifecycle.backend_done

    @projected(view="snapshot", name="process_dead")
    def snapshot_process_dead(self):
        return self.lifecycle.process_dead

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        for field, value in (
            ("owner_lookup", self.owner_lookup),
            ("owner_token_digest", self.owner_token_digest),
        ):
            require_nonempty(value, field)
            require_bounded(value, field, MAX_IDENTIFIER_CHARS)
        if min(self.attempt_ordinal, self.owner_generation, self.revision) <= 0:
            raise ValueError("attempt identity/generation/revision must be positive")
        if self.created_at_ms < 0 or self.updated_at_ms < self.created_at_ms:
            raise ValueError("attempt timestamps are inconsistent")
        if self.last_progress_at_ms is not None and not (
            self.created_at_ms <= self.last_progress_at_ms <= self.updated_at_ms
        ):
            raise ValueError("attempt progress time is inconsistent")

    phase: ClassVar[Generated] = Generated(
        str,
        Column(generated="json_extract(lifecycle, '$.kind')", check="phase IN ({attempt_names})"),
    )
    lease_expires_at_ms: ClassVar[Generated] = Generated(
        int,
        Column(
            generated="json_extract(lifecycle, '$.lease_expires_at_ms')",
            check="lease_expires_at_ms >= 0",
            nullable=True,
        ),
    )
    backend_done: ClassVar[Generated] = Generated(
        bool,
        Column(
            generated="CASE WHEN json_extract(lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(lifecycle, '$.backend_done') END",
            check="backend_done IN (0,1)",
        ),
    )
    process_dead: ClassVar[Generated] = Generated(
        bool,
        Column(
            generated="CASE WHEN json_extract(lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(lifecycle, '$.process_dead') END",
            check="process_dead IN (0,1)",
        ),
    )
    phase_kind: ClassVar[Generated] = Generated(
        str,
        Column(
            generated=(
                "CASE\n       WHEN phase IN ({terminal_attempt_names}) THEN phase ELSE 'active' END"
            ),
            nullable=True,
        ),
    )
    active_required_status: ClassVar[Generated] = Generated(
        str,
        Column(generated="CASE WHEN phase_kind = 'active' THEN 'active' END", nullable=True),
    )
    checks = (
        (
            "last_progress_at_ms IS NULL OR\n"
            "           last_progress_at_ms BETWEEN created_at_ms AND updated"
            "_at_ms"
        ),
        (
            "(phase_kind = 'active' AND lease_expires_at_ms IS NOT NULL)\n"
            "       OR (phase_kind != 'active' AND lease_expires_at_ms IS NUL"
            "L\n"
            "           AND backend_done = 1 AND process_dead = 1)"
        ),
    )
    unique = (("execution_id", "attempt_ordinal", "owner_lookup", "phase_kind"),)

    @classmethod
    def references(cls):
        from agent_comms.coordination_tables.executions import ExecutionRecord
        from agent_comms.coordination_tables.participants import Participants

        return (
            ForeignKey(columns=("execution_id",), target=ExecutionRecord, target_columns=("execution_id",)),
            ForeignKey(columns=("owner_lookup",), target=Participants, target_columns=("participant_lookup",)),
            ForeignKey(
                columns=("execution_id", "attempt_ordinal", "owner_lookup", "active_required_status"),
                target=ExecutionRecord,
                target_columns=("execution_id", "current_attempt_ordinal", "owner_lookup", "status"),
                deferred=True,
            ),
        )
