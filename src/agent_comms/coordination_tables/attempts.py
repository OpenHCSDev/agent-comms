"""Attempt rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from enum import IntFlag
from typing import Final

from agent_comms.attempt_states import AttemptState
from agent_comms.coordination_contracts import (
    MAX_IDENTIFIER_CHARS,
    MAX_REASON_CODE_CHARS,
    require_bounded,
    require_nonempty,
    require_optional_nonempty,
    validate_execution_id,
)
from agent_comms.coordination_errors import IntegrityViolationError, IdentityConflict
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.coordination_tables.participants import OwnerGenerations
from agent_comms.field_codec import projected
from agent_comms.owner_fence import AttemptAuthority
from agent_comms.typed_table import (
    Column,
    ForeignKey,
    TypedTable,
)


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


@dataclass(frozen=True, slots=True)
class AttemptRecord(CoordinatorTable, TypedTable, declared_name="attempts"):
    def advance(
        self,
        session,
        phase: type[AttemptState],
        *,
        backend_done: bool,
        process_dead: bool,
        progress: bool,
        reason_code: str | None,
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
            self.owner_thread,
            self.owner_generation,
            self.owner_token_digest,
        )

    @property
    def owner_identity(self) -> OwnerGenerations:
        return OwnerGenerations(
            owner_lookup=self.owner_lookup,
            owner_thread=self.owner_thread,
            generation=self.owner_generation,
        )

    execution_id: str = dataclass_field(
        metadata={"snapshot_exclude": True, "sql": Column(primary_key=True)}
    )
    attempt_ordinal: int = dataclass_field(
        metadata={"sql": Column(primary_key=True, check="attempt_ordinal > 0")}
    )
    owner_lookup: str
    owner_thread: str = dataclass_field(
        metadata={"sql": Column(check="length(owner_thread) BETWEEN 1 AND 256")}
    )
    owner_generation: int = dataclass_field(metadata={"sql": Column(check="owner_generation > 0")})
    owner_token_digest: str = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(unique=True, check="length(owner_token_digest) BETWEEN 1 AND 256"),
        }
    )
    lifecycle: AttemptState = dataclass_field(metadata={"snapshot_exclude": True})
    revision: int = dataclass_field(metadata={"sql": Column(check="revision > 0")})
    last_progress_at_ms: int | None
    reason_code: str | None = dataclass_field(
        metadata={
            "sql": Column(check="reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 64")
        }
    )
    created_at_ms: int = dataclass_field(metadata={"sql": Column(check="created_at_ms >= 0")})
    updated_at_ms: int = dataclass_field(
        metadata={"sql": Column(check="updated_at_ms >= created_at_ms")}
    )

    @projected(view="snapshot", name="phase")
    def snapshot_phase(self):
        return self.lifecycle.declared_name

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
            ("owner_thread", self.owner_thread),
            ("owner_token_digest", self.owner_token_digest),
        ):
            require_nonempty(value, field)
            require_bounded(value, field, MAX_IDENTIFIER_CHARS)
        if min(self.attempt_ordinal, self.owner_generation, self.revision) <= 0:
            raise ValueError("attempt identity/generation/revision must be positive")
        require_optional_nonempty(self.reason_code, "reason_code", MAX_REASON_CODE_CHARS)
        if self.created_at_ms < 0 or self.updated_at_ms < self.created_at_ms:
            raise ValueError("attempt timestamps are inconsistent")
        if self.last_progress_at_ms is not None and not (
            self.created_at_ms <= self.last_progress_at_ms <= self.updated_at_ms
        ):
            raise ValueError("attempt progress time is inconsistent")

    phase: str = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.kind')", check="phase IN ({attempt_names})"
            ),
        },
    )
    lease_expires_at_ms: int | None = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.lease_expires_at_ms')",
                check="lease_expires_at_ms >= 0",
            ),
        },
    )
    backend_done: bool = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="CASE WHEN json_extract(lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(lifecycle, '$.backend_done') END",
                check="backend_done IN (0,1)",
            ),
        },
    )
    process_dead: bool = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="CASE WHEN json_extract(lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(lifecycle, '$.process_dead') END",
                check="process_dead IN (0,1)",
            ),
        },
    )
    phase_kind: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated=(
                    "CASE\n"
                    "       WHEN phase IN ({terminal_attempt_names}) THEN phase ELSE "
                    "'active' END"
                )
            ),
        },
    )
    active_required_status: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(generated="CASE WHEN phase_kind = 'active' THEN 'active' END"),
        },
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
            ForeignKey(("execution_id",), ExecutionRecord, ("execution_id",)),
            ForeignKey(("owner_lookup",), Participants, ("participant_lookup",)),
            ForeignKey(
                ("execution_id", "attempt_ordinal", "owner_lookup", "active_required_status"),
                ExecutionRecord,
                ("execution_id", "current_attempt_ordinal", "owner_lookup", "status"),
                deferred=True,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "attempt_insert_authorized": (
                """CREATE TRIGGER attempt_insert_authorized BEFORE INSERT ON attempts BEGIN
    SELECT RAISE(ABORT, 'attempt must be contiguous and within execution budget')
    WHERE NOT EXISTS (
        SELECT 1 FROM executions e WHERE e.execution_id = NEW.execution_id
        AND e.owner_lookup = NEW.owner_lookup AND NEW.attempt_ordinal <= e.max_attempts
        AND ((NEW.attempt_ordinal = 1 AND
              (e.status = 'pending' OR
               (e.status = 'active' AND e.current_attempt_ordinal = 1)))
          OR (NEW.attempt_ordinal > 1 AND
              (e.status = 'deferred' OR
               (e.status = 'active' AND e.current_attempt_ordinal = NEW.attempt_ordinal))))
        AND NEW.attempt_ordinal = 1 + coalesce(
            (SELECT max(a.attempt_ordinal) FROM attempts a
              WHERE a.execution_id = e.execution_id), 0));
    SELECT RAISE(ABORT, 'attempt generation must match owner counter')
    WHERE NOT EXISTS (SELECT 1 FROM owner_generations g
      WHERE g.owner_lookup = NEW.owner_lookup AND g.owner_thread = NEW.owner_thread
        AND g.generation = NEW.owner_generation);
    SELECT RAISE(ABORT, 'new attempt requires a fresh active fence')
    WHERE (json_extract(NEW.lifecycle, '$.kind')) != 'prompt_starting' OR NEW.revision != 1
       OR (CASE WHEN json_extract(NEW.lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(NEW.lifecycle, '$.backend_done') END) != 0 OR (CASE WHEN json_extract(NEW.lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(NEW.lifecycle, '$.process_dead') END) != 0;
    SELECT RAISE(ABORT, 'retry requires derived authorization and no current owner')
    WHERE NEW.attempt_ordinal > 1 AND (
      NOT EXISTS (SELECT 1 FROM retry_disposition_basis b
        WHERE b.execution_id = NEW.execution_id AND b.authorized = 1)
      OR EXISTS (SELECT 1 FROM current_executions p
        WHERE p.owner_lookup = NEW.owner_lookup AND p.execution_id IS NOT NULL));
    SELECT RAISE(ABORT, 'previous attempt must be dead with an older distinct fence')
    WHERE NEW.attempt_ordinal > 1 AND NOT EXISTS (
      SELECT 1 FROM attempts a WHERE a.execution_id = NEW.execution_id
      AND a.attempt_ordinal = NEW.attempt_ordinal - 1
      AND a.phase = 'attempt_failed' AND a.backend_done = 1 AND a.process_dead = 1
      AND NEW.owner_generation > a.owner_generation
      AND NEW.owner_token_digest != a.owner_token_digest);
END"""
            ),
            "attempt_phase_edge": (
                """CREATE TRIGGER attempt_phase_edge BEFORE UPDATE OF lifecycle ON attempts
WHEN (json_extract(NEW.lifecycle, '$.kind')) != (json_extract(OLD.lifecycle, '$.kind')) AND NOT ({attempt_edges})
BEGIN SELECT RAISE(ABORT, 'attempt phase transition is not declared'); END"""
            ),
            "attempt_frozen_facts": (
                """CREATE TRIGGER attempt_frozen_facts BEFORE UPDATE ON attempts
WHEN NEW.execution_id IS NOT OLD.execution_id
 OR NEW.attempt_ordinal != OLD.attempt_ordinal
 OR NEW.owner_lookup IS NOT OLD.owner_lookup OR NEW.owner_thread IS NOT OLD.owner_thread
 OR NEW.owner_generation != OLD.owner_generation
 OR NEW.owner_token_digest IS NOT OLD.owner_token_digest
 OR NEW.created_at_ms != OLD.created_at_ms
 OR NEW.revision != OLD.revision + 1
 OR NEW.updated_at_ms < OLD.updated_at_ms
 OR ((CASE WHEN json_extract(OLD.lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(OLD.lifecycle, '$.backend_done') END) = 1 AND (CASE WHEN json_extract(NEW.lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(NEW.lifecycle, '$.backend_done') END) = 0)
 OR ((CASE WHEN json_extract(OLD.lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(OLD.lifecycle, '$.process_dead') END) = 1 AND (CASE WHEN json_extract(NEW.lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(NEW.lifecycle, '$.process_dead') END) = 0)
 OR (OLD.last_progress_at_ms IS NOT NULL AND
      (NEW.last_progress_at_ms IS NULL OR NEW.last_progress_at_ms < OLD.last_progress_at_ms))
 OR ((json_extract(OLD.lifecycle, '$.lease_expires_at_ms')) IS NOT NULL AND (json_extract(NEW.lifecycle, '$.lease_expires_at_ms')) IS NOT NULL
      AND (json_extract(NEW.lifecycle, '$.lease_expires_at_ms')) < (json_extract(OLD.lifecycle, '$.lease_expires_at_ms')))
 OR ((json_extract(OLD.lifecycle, '$.kind')) IN ({terminal_attempt_names}) AND (json_extract(NEW.lifecycle, '$.kind')) = (json_extract(OLD.lifecycle, '$.kind')))
BEGIN SELECT RAISE(ABORT, 'attempt transition rewrites frozen facts'); END"""
            ),
            "attempt_delete_frozen": (
                """CREATE TRIGGER attempt_delete_frozen BEFORE DELETE ON attempts BEGIN
    SELECT RAISE(ABORT, 'attempt cannot be deleted');
END"""
            ),
        }


@dataclass(frozen=True, slots=True)
class ReplayAssessments(CoordinatorTable, TypedTable):
    @property
    def allows_retry(self) -> bool:
        return self.replay_safe and self.facts == ReplayFact.NONE and not self.side_effects_possible

    execution_id: str = dataclass_field(
        metadata={"snapshot_exclude": True, "sql": Column(primary_key=True)}
    )
    facts: ReplayFact = dataclass_field(
        metadata={"sql": Column(check="facts >= 0 AND facts <= 1023")}
    )
    replay_safe: bool = dataclass_field(metadata={"sql": Column()})
    side_effects_possible: bool = dataclass_field(metadata={"sql": Column()})
    revision: int = dataclass_field(metadata={"sql": Column(check="revision > 0")})

    @classmethod
    def accumulate(cls, db, execution_id: str, observed: ReplayFact) -> None:
        """Join original observations under the caller's existing attempt fence.

        Missing replay evidence cannot establish safety. Only an existing
        assessment may retain that fact, and every incoming observation can
        revoke it. The monotonic record and SQL constraints own the write.
        """
        before = cls.one(db, execution_id=execution_id)
        if before is None:
            if not observed:
                return
            after = cls(execution_id, observed, False, True, 1)
        else:
            facts = before.facts | observed
            after = replace(
                before,
                facts=facts,
                replay_safe=before.replay_safe and not facts,
                side_effects_possible=before.side_effects_possible or bool(observed),
                revision=before.revision + 1,
            )
        after.record(db, before)

    @classmethod
    def revoke_for_retry(cls, db, execution_id: str) -> None:
        """A new fenced attempt cannot reuse the prior attempt's safety proof."""
        before = cls.one(db, execution_id=execution_id)
        if before is not None and before.replay_safe:
            replace(before, replay_safe=False, revision=before.revision + 1).record(db, before)

    def require_successor(self, after: ReplayAssessments) -> None:
        accumulated = replace(
            after,
            facts=self.facts | after.facts,
            replay_safe=self.replay_safe and after.replay_safe,
            side_effects_possible=self.side_effects_possible or after.side_effects_possible,
        )
        if accumulated != after:
            raise IdentityConflict("replay facts cannot be erased")

    def record(self, db, before: ReplayAssessments | None) -> bool:
        """Persist monotonic replay evidence inside the checked transaction."""

        if before is not None:
            if replace(before, revision=self.revision) == self:
                return False
            before.require_successor(self)
            ReplayAssessments.update(
                db,
                where="execution_id=?",
                parameters=(self.execution_id,),
                facts=self.facts,
                replay_safe=self.replay_safe,
                side_effects_possible=self.side_effects_possible,
                revision=before.revision + 1,
            )
        else:
            self.insert(db)
        return True

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        if self.revision <= 0:
            raise ValueError("replay revision must be positive")
        object.__setattr__(self, "facts", ReplayFact(self.facts))
        if int(self.facts) < 0 or int(self.facts) & ~APPROVED_REPLAY_FACT_MASK:
            raise ValueError("unrecognized replay fact bits")
        if self.facts != ReplayFact.NONE and self.replay_safe:
            raise IntegrityViolationError("an execution with ambiguity facts cannot be replay-safe")

    retry_authorized: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "sql": Column(
                generated=(
                    "CASE WHEN facts = 0 AND replay_safe = 1 AND side_effects_possibl"
                    "e = 0\n"
                    "              THEN 1 ELSE 0 END"
                )
            )
        },
    )

    checks = ("facts = 0 OR replay_safe = 0",)

    unique = (("execution_id", "retry_authorized"),)

    @classmethod
    def references(cls):
        from agent_comms.coordination_tables.executions import ExecutionRecord

        return (
            ForeignKey(("execution_id",), ExecutionRecord, ("execution_id",), on_delete="RESTRICT"),
        )

    @classmethod
    def triggers(cls):
        return {
            "replay_assessment_monotonic": (
                """CREATE TRIGGER replay_assessment_monotonic
BEFORE UPDATE ON replay_assessments
WHEN NEW.execution_id IS NOT OLD.execution_id
 OR NEW.revision != OLD.revision + 1
 OR (NEW.facts | OLD.facts) != NEW.facts
 OR (OLD.replay_safe = 0 AND NEW.replay_safe = 1)
 OR (OLD.side_effects_possible = 1 AND NEW.side_effects_possible = 0)
BEGIN
    SELECT RAISE(ABORT, 'replay assessment cannot erase ambiguity');
END"""
            ),
            "replay_assessment_delete_frozen": (
                """CREATE TRIGGER replay_assessment_delete_frozen BEFORE DELETE ON
replay_assessments BEGIN
    SELECT RAISE(ABORT, 'replay assessment cannot be deleted' );
END"""
            ),
            "failed_retry_partition_replay_insert": (
                """CREATE TRIGGER failed_retry_partition_replay_insert AFTER INSERT ON
replay_assessments
WHEN EXISTS (SELECT 1 FROM retry_disposition_basis b JOIN executions e
             ON e.execution_id = b.execution_id
             WHERE b.execution_id = NEW.execution_id AND b.authorized
             = 1
               AND e.status = 'failed' AND e.current_attempt_ordinal
               IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'authorized retry cannot settle failed' );
END"""
            ),
            "failed_retry_partition_replay_update": (
                """CREATE TRIGGER failed_retry_partition_replay_update AFTER UPDATE ON
replay_assessments
WHEN EXISTS (SELECT 1 FROM retry_disposition_basis b JOIN executions e
             ON e.execution_id = b.execution_id
             WHERE b.execution_id = NEW.execution_id AND b.authorized
             = 1
               AND e.status = 'failed' AND e.current_attempt_ordinal
               IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'authorized retry cannot settle failed' );
END"""
            ),
        }
