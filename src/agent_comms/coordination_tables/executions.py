"""Execution rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum

from agent_comms.coordination_contracts import (
    MAX_IDENTIFIER_CHARS,
    MAX_REASON_CODE_CHARS,
    require_bounded,
    require_nonempty,
    require_optional_nonempty,
    validate_execution_id,
)
from agent_comms.coordination_errors import IntegrityViolationError
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.coordination_tables.attempts import AttemptRecord
from agent_comms.execution_states import ExecutionState
from agent_comms.field_codec import projected
from agent_comms.typed_table import (
    Column,
    ForeignKey,
    Index,
    TypedTable,
)


class ExecutionOrigin(StrEnum):
    WIRE = "wire"
    ACP = "acp"
    GOAL = "goal"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class CurrentExecutions(CoordinatorTable, TypedTable):
    def require_idle(self) -> None:
        from agent_comms.coordination_errors import StaleFence

        if self.execution_id is not None:
            raise StaleFence(
                "selected owner has an unresolved execution; new claims remain pending"
            )

    owner_lookup: str = dataclass_field(metadata={"sql": Column(primary_key=True)})
    execution_id: str | None = dataclass_field(metadata={"sql": Column()})
    attempt_ordinal: int | None = dataclass_field(metadata={"sql": Column()})
    pointer_revision: int = dataclass_field(metadata={"sql": Column(check="pointer_revision >= 0")})

    def __post_init__(self) -> None:
        require_nonempty(self.owner_lookup, "owner_lookup")
        require_bounded(self.owner_lookup, "owner_lookup", MAX_IDENTIFIER_CHARS)
        if (self.execution_id is None) != (self.attempt_ordinal is None):
            raise IntegrityViolationError("pointer execution and ordinal must pair")
        if self.execution_id is not None:
            validate_execution_id(self.execution_id)
            if self.attempt_ordinal is None or self.attempt_ordinal <= 0:
                raise ValueError("pointer ordinal must be positive")
        if self.pointer_revision < 0:
            raise ValueError("pointer_revision cannot be negative")

    required_active: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={"sql": Column(generated="CASE WHEN execution_id IS NOT NULL THEN 'active' END")},
    )

    checks = ("(execution_id IS NULL) = (attempt_ordinal IS NULL)",)

    unique = (("owner_lookup", "execution_id", "attempt_ordinal"),)

    @classmethod
    def references(cls):
        from agent_comms.coordination_tables.participants import Participants

        return (
            ForeignKey(("owner_lookup",), Participants, ("participant_lookup",)),
            ForeignKey(
                ("execution_id", "attempt_ordinal", "owner_lookup", "required_active"),
                ExecutionRecord,
                ("execution_id", "current_attempt_ordinal", "owner_lookup", "status"),
                deferred=True,
            ),
            ForeignKey(
                ("execution_id", "attempt_ordinal", "owner_lookup", "required_active"),
                AttemptRecord,
                ("execution_id", "attempt_ordinal", "owner_lookup", "phase_kind"),
                deferred=True,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "current_pointer_insert_owner": (
                """CREATE TRIGGER current_pointer_insert_owner BEFORE INSERT ON current_executions
WHEN NOT EXISTS (SELECT 1 FROM owner_generations
 WHERE owner_lookup = NEW.owner_lookup)
BEGIN SELECT RAISE(ABORT, 'current pointer owner is not registered'); END"""
            ),
            "current_pointer_update_owner": (
                """CREATE TRIGGER current_pointer_update_owner BEFORE UPDATE ON current_executions
WHEN NEW.owner_lookup IS NOT OLD.owner_lookup
 OR NEW.pointer_revision != OLD.pointer_revision + 1
BEGIN SELECT RAISE(ABORT, 'current pointer owner/revision mismatch'); END"""
            ),
            "current_pointer_delete_frozen": (
                """CREATE TRIGGER current_pointer_delete_frozen BEFORE DELETE ON
current_executions BEGIN
    SELECT RAISE(ABORT, 'current pointer cannot be deleted' );
END"""
            ),
        }


@dataclass(frozen=True, slots=True)
class ExecutionRecord(CoordinatorTable, TypedTable, declared_name="executions"):
    execution_id: str = dataclass_field(
        metadata={
            "sql": Column(
                primary_key=True,
                check=(
                    "\n"
                    "        length(execution_id) BETWEEN 1 AND 256 AND instr(executi"
                    "on_id, ':') = 0\n"
                    "    "
                ),
            )
        }
    )
    origin: ExecutionOrigin
    lifecycle: ExecutionState = dataclass_field(metadata={"snapshot_exclude": True})
    owner_thread: str = dataclass_field(
        metadata={"sql": Column(check="length(owner_thread) BETWEEN 1 AND 256")}
    )
    owner_lookup: str
    revision: int = dataclass_field(metadata={"sql": Column(check="revision > 0")})
    max_attempts: int = dataclass_field(metadata={"sql": Column(check="max_attempts > 0")})
    reason_code: str | None = dataclass_field(
        metadata={
            "sql": Column(check="reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 64")
        }
    )
    created_at_ms: int = dataclass_field(metadata={"sql": Column(check="created_at_ms >= 0")})
    updated_at_ms: int = dataclass_field(
        metadata={"sql": Column(check="updated_at_ms >= created_at_ms")}
    )
    exact_target: str | None = dataclass_field(
        default=None,
        metadata={
            "sql": Column(check="exact_target IS NULL OR length(exact_target) BETWEEN 1 AND 256")
        },
    )

    @projected(view="snapshot", name="status")
    def snapshot_status(self):
        return self.lifecycle.declared_name

    @projected(view="snapshot", name="current_attempt_ordinal")
    def snapshot_current_attempt_ordinal(self):
        return self.lifecycle.current_attempt_ordinal

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        object.__setattr__(self, "origin", ExecutionOrigin(self.origin))
        for field, value in (
            ("owner_thread", self.owner_thread),
            ("owner_lookup", self.owner_lookup),
        ):
            require_nonempty(value, field)
            require_bounded(value, field, MAX_IDENTIFIER_CHARS)
        if self.revision <= 0 or self.max_attempts <= 0:
            raise ValueError("revision and max_attempts must be positive")
        self.lifecycle.validate_budget(self.max_attempts)
        require_optional_nonempty(self.reason_code, "reason_code", MAX_REASON_CODE_CHARS)
        if self.created_at_ms < 0 or self.updated_at_ms < self.created_at_ms:
            raise ValueError("execution timestamps are inconsistent")
        if self.origin is ExecutionOrigin.WIRE:
            if self.exact_target is None:
                raise IntegrityViolationError("wire execution requires an exact target")
            require_nonempty(self.exact_target, "exact_target")
            require_bounded(self.exact_target, "exact_target", MAX_IDENTIFIER_CHARS)
        elif self.exact_target is not None:
            raise IntegrityViolationError("claimless execution requires null target")

    status: str = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.kind')",
                check="status IN\n      ({execution_names})",
            ),
        },
    )
    current_attempt_ordinal: int | None = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.ordinal')",
                check=(
                    "\n"
                    "        current_attempt_ordinal IS NULL OR current_attempt_ordin"
                    "al BETWEEN 1 AND max_attempts\n"
                    "    "
                ),
            ),
        },
    )
    required_attempt_kind: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated=(
                    "CASE\n"
                    "        WHEN status = 'active' THEN 'active'\n"
                    "        WHEN status = 'completed' THEN 'succeeded'\n"
                    "        WHEN status IN ({optional_attempt_execution_names}) AND "
                    "current_attempt_ordinal IS NOT NULL\n"
                    "          THEN 'attempt_failed'\n"
                    "    END"
                )
            ),
        },
    )
    active_execution_id: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(generated="CASE WHEN status = 'active' THEN execution_id END"),
        },
    )
    active_attempt_ordinal: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(generated="CASE WHEN status = 'active' THEN current_attempt_ordinal END"),
        },
    )
    wire_execution_id: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(generated="CASE WHEN origin = 'wire' THEN execution_id END"),
        },
    )
    wire_claim_ordinal: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(generated="CASE WHEN origin = 'wire' THEN 0 END"),
        },
    )
    claim_status_kind: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated=(
                    "CASE\n"
                    "        WHEN status IN ({engaged_execution_names}) THEN 'engaged"
                    "'\n"
                    "        ELSE status END"
                )
            ),
        },
    )
    completed_wire_id: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="CASE WHEN status = 'completed' AND origin = 'wire' THEN execution_id END"
            ),
        },
    )
    required_obligation_terminal: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="CASE WHEN status = 'completed' AND origin = 'wire' THEN 1 END"
            ),
        },
    )
    deferred_replay_id: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated=(
                    "CASE WHEN status = 'deferred' AND current_attempt_ordinal IS NOT"
                    " NULL\n"
                    "              THEN execution_id END"
                )
            ),
        },
    )
    deferred_replay_required: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated=(
                    "CASE WHEN status = 'deferred' AND current_attempt_ordinal IS NOT"
                    " NULL\n"
                    "              THEN 1 END"
                )
            ),
        },
    )
    deferred_obligation_id: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated=(
                    "CASE WHEN status = 'deferred' AND current_attempt_ordinal IS NOT"
                    " NULL\n"
                    "                   AND origin = 'wire' THEN execution_id END"
                )
            ),
        },
    )
    deferred_obligation_required: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated=(
                    "CASE WHEN status = 'deferred' AND current_attempt_ordinal IS NOT"
                    " NULL\n"
                    "                   AND origin = 'wire' THEN 1 END"
                )
            ),
        },
    )
    checks = (
        (
            "(status IN ({unstarted_execution_names}) AND current_attempt_ord"
            "inal IS NULL)\n"
            "      OR (status IN ({required_attempt_execution_names}) AND cur"
            "rent_attempt_ordinal IS NOT NULL)\n"
            "      OR status IN ({optional_attempt_execution_names})"
        ),
        (
            "status != 'deferred' OR current_attempt_ordinal IS NULL\n"
            "           OR current_attempt_ordinal < max_attempts"
        ),
        (
            "(origin = 'wire' AND exact_target IS NOT NULL)\n"
            "      OR (origin != 'wire' AND exact_target IS NULL)"
        ),
    )
    unique = (
        ("execution_id", "owner_lookup"),
        ("execution_id", "claim_status_kind"),
        ("execution_id", "current_attempt_ordinal", "owner_lookup", "status"),
    )
    indexes = (Index(("owner_lookup", "status"), unique=False, where=None),)

    @classmethod
    def references(cls):
        from agent_comms.coordination_tables.assignments import ExecutionAssignmentLink
        from agent_comms.coordination_tables.attempts import ReplayAssessments
        from agent_comms.coordination_tables.participants import Participants
        from agent_comms.coordination_tables.responses import ResponseObligation

        return (
            ForeignKey(("owner_lookup",), Participants, ("participant_lookup",)),
            ForeignKey(
                (
                    "execution_id",
                    "current_attempt_ordinal",
                    "owner_lookup",
                    "required_attempt_kind",
                ),
                AttemptRecord,
                ("execution_id", "attempt_ordinal", "owner_lookup", "phase_kind"),
                deferred=True,
            ),
            ForeignKey(
                ("owner_lookup", "active_execution_id", "active_attempt_ordinal"),
                CurrentExecutions,
                ("owner_lookup", "execution_id", "attempt_ordinal"),
                deferred=True,
            ),
            ForeignKey(
                ("wire_execution_id", "wire_claim_ordinal"),
                ExecutionAssignmentLink,
                ("execution_id", "ordinal"),
                deferred=True,
            ),
            ForeignKey(
                ("wire_execution_id",), ResponseObligation, ("execution_id",), deferred=True
            ),
            ForeignKey(
                ("completed_wire_id", "required_obligation_terminal"),
                ResponseObligation,
                ("execution_id", "success_terminal"),
                deferred=True,
            ),
            ForeignKey(
                ("deferred_replay_id", "deferred_replay_required"),
                ReplayAssessments,
                ("execution_id", "retry_authorized"),
                deferred=True,
            ),
            ForeignKey(
                ("deferred_obligation_id", "deferred_obligation_required"),
                ResponseObligation,
                ("execution_id", "retryable"),
                deferred=True,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "execution_status_edge": (
                """CREATE TRIGGER execution_status_edge BEFORE UPDATE OF lifecycle ON executions
WHEN json_extract(NEW.lifecycle, '$.kind') != json_extract(OLD.lifecycle, '$.kind') AND NOT ({execution_edges})
BEGIN SELECT RAISE(ABORT, 'execution status transition is not declared'); END"""
            ),
            "execution_frozen_facts": (
                """CREATE TRIGGER execution_frozen_facts BEFORE UPDATE ON executions
WHEN NEW.execution_id IS NOT OLD.execution_id OR NEW.origin IS NOT OLD.origin
 OR NEW.exact_target IS NOT OLD.exact_target OR NEW.owner_lookup IS NOT OLD.owner_lookup
 OR NEW.owner_thread IS NOT OLD.owner_thread OR NEW.max_attempts != OLD.max_attempts
 OR NEW.created_at_ms != OLD.created_at_ms OR NEW.revision != OLD.revision + 1
 OR NEW.updated_at_ms < OLD.updated_at_ms OR
 (json_extract(NEW.lifecycle, '$.kind') = 'active' AND
  (json_extract(OLD.lifecycle, '$.kind') NOT IN ({startable_execution_names}) OR
   json_extract(NEW.lifecycle, '$.ordinal') != coalesce(json_extract(OLD.lifecycle, '$.ordinal'), 0) + 1)) OR
 (json_extract(NEW.lifecycle, '$.kind') != 'active' AND
  json_extract(NEW.lifecycle, '$.ordinal') IS NOT json_extract(OLD.lifecycle, '$.ordinal'))
BEGIN SELECT RAISE(ABORT, 'execution transition rewrites frozen authority'); END"""
            ),
            "execution_failure_receipt_guard": (
                """CREATE TRIGGER execution_failure_receipt_guard BEFORE UPDATE OF lifecycle
ON executions
WHEN json_extract(NEW.lifecycle, '$.kind') = 'failed' AND EXISTS (
  SELECT 1 FROM publication_receipts WHERE execution_id =
  NEW.execution_id)
BEGIN SELECT RAISE(ABORT,
'failed execution cannot erase publication receipt' ); END"""
            ),
            "execution_delete_frozen": (
                """CREATE TRIGGER execution_delete_frozen BEFORE DELETE ON executions BEGIN
    SELECT RAISE(ABORT, 'execution cannot be deleted');
END"""
            ),
            "failed_retry_partition_update": (
                """CREATE TRIGGER failed_retry_partition_update BEFORE UPDATE OF lifecycle
ON executions
WHEN json_extract(NEW.lifecycle, '$.kind') = 'failed' AND json_extract(NEW.lifecycle, '$.ordinal') IS NOT NULL
 AND EXISTS (SELECT 1 FROM retry_disposition_basis b
             WHERE b.execution_id = NEW.execution_id AND b.authorized
             = 1)
BEGIN SELECT RAISE(ABORT, 'authorized retry cannot settle failed' );
END"""
            ),
        }
