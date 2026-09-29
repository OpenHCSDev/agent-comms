"""Assignment rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum

from agent_comms.assignment_states import AssignmentState
from agent_comms.coordination_contracts import (
    MAX_IDENTIFIER_CHARS,
    POLICY_VERSION,
    RESOLVER_VERSION,
    require_bounded,
    require_nonempty,
    require_optional_nonempty,
    validate_execution_id,
)
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.field_codec import projected
from agent_comms.typed_table import (
    Column,
    ForeignKey,
    Index,
    TypedTable,
)


class MessageAudience(StrEnum):
    DIRECT = "direct"
    MENTIONED = "mentioned"
    COLLECTIVE = "collective"


@dataclass(frozen=True, slots=True)
class WakeAssignment(CoordinatorTable, TypedTable, declared_name="wake_claims"):
    assignment_id: str = dataclass_field(
        metadata={
            "wire_name": "claim_id",
            "sql": Column(primary_key=True, check="length(assignment_id) BETWEEN 1 AND 256"),
        }
    )
    recipient: str = dataclass_field(
        metadata={"sql": Column(check="length(recipient) BETWEEN 1 AND 256")}
    )
    recipient_lookup: str
    wire_seq: int = dataclass_field(metadata={"sql": Column(check="wire_seq > 0")})
    message_id: str = dataclass_field(
        metadata={"sql": Column(check="length(message_id) BETWEEN 1 AND 256")}
    )
    audience: MessageAudience
    lifecycle: AssignmentState = dataclass_field(metadata={"snapshot_exclude": True})
    accepted_at_ms: int = dataclass_field(metadata={"sql": Column(check="accepted_at_ms >= 0")})
    updated_at_ms: int = dataclass_field(
        metadata={"sql": Column(check="updated_at_ms >= accepted_at_ms")}
    )
    revision: int = dataclass_field(default=1, metadata={"sql": Column(check="revision > 0")})
    resolver_version: str = dataclass_field(
        default=RESOLVER_VERSION,
        metadata={"sql": Column(check="length(resolver_version) BETWEEN 1 AND 256")},
    )
    policy_version: str = dataclass_field(
        default=POLICY_VERSION,
        metadata={"sql": Column(check="length(policy_version) BETWEEN 1 AND 256")},
    )

    @projected(view="snapshot", name="disposition")
    def snapshot_disposition(self):
        return self.lifecycle.declared_name

    @projected(view="snapshot", name="wake_mode")
    def snapshot_wake_mode(self):
        return self.lifecycle.mode.declared_name

    @projected(view="snapshot", name="triage_verdict")
    def snapshot_triage_verdict(self):
        verdict = self.lifecycle.verdict
        return verdict

    @projected(view="snapshot", name="execution_id")
    def snapshot_execution_id(self):
        return self.lifecycle.execution_id

    @projected(view="snapshot", name="exact_target")
    def snapshot_exact_target(self):
        return self.lifecycle.exact_target

    def __post_init__(self) -> None:
        for field, value in (
            ("claim_id", self.assignment_id),
            ("recipient", self.recipient),
            ("recipient_lookup", self.recipient_lookup),
            ("message_id", self.message_id),
            ("resolver_version", self.resolver_version),
            ("policy_version", self.policy_version),
        ):
            require_nonempty(value, field)
            require_bounded(value, field, MAX_IDENTIFIER_CHARS)
        require_optional_nonempty(self.lifecycle.exact_target, "exact_target", MAX_IDENTIFIER_CHARS)
        if self.lifecycle.execution_id is not None:
            validate_execution_id(self.lifecycle.execution_id)
        if self.wire_seq <= 0 or self.revision <= 0:
            raise ValueError("wire_seq and revision must be positive")
        if self.accepted_at_ms < 0 or self.updated_at_ms < self.accepted_at_ms:
            raise ValueError("claim timestamps are inconsistent")
        object.__setattr__(self, "audience", MessageAudience(self.audience))

    @property
    def durable_key(self) -> tuple[str, int]:
        return (self.recipient_lookup, self.wire_seq)

    exact_target: str | None = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.decision.exact_target')",
                check="exact_target IS NULL OR length(exact_target) BETWEEN 1 AND 256",
            ),
        },
    )
    wake_mode: str = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(generated="({assignment_mode})", check="wake_mode IN ({wake_names})"),
        },
    )
    triage_verdict: str | None = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="({assignment_verdict})",
                check="triage_verdict IS NULL OR triage_verdict IN ('ignore', 'engage')",
            ),
        },
    )
    disposition: str = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.kind')",
                check="disposition IN ({assignment_names})",
            ),
        },
    )
    execution_id: str | None = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.decision.execution_id')",
            ),
        },
    )
    claim_status_kind: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(generated="CASE WHEN execution_id IS NOT NULL THEN disposition END"),
        },
    )
    checks = ("COALESCE(({assignment_binding}), 0)",)
    unique = (("recipient_lookup", "wire_seq"), ("assignment_id", "execution_id"))
    indexes = (Index(("execution_id",), unique=False, where=None),)

    @classmethod
    def references(cls):
        from agent_comms.coordination_tables.executions import ExecutionRecord
        from agent_comms.coordination_tables.participants import Participants

        return (
            ForeignKey(("recipient_lookup",), Participants, ("participant_lookup",)),
            ForeignKey(("execution_id",), ExecutionRecord, ("execution_id",)),
            ForeignKey(
                ("execution_id", "assignment_id"),
                ExecutionAssignmentLink,
                ("execution_id", "assignment_id"),
                deferred=True,
            ),
            ForeignKey(
                ("execution_id", "claim_status_kind"),
                ExecutionRecord,
                ("execution_id", "claim_status_kind"),
                deferred=True,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "claim_transition_frozen_facts": (
                """CREATE TRIGGER claim_transition_frozen_facts
BEFORE UPDATE ON wake_claims
BEGIN
    SELECT RAISE(ABORT, 'claim disposition must change')
    WHERE (json_extract(NEW.lifecycle, '$.kind')) = (json_extract(OLD.lifecycle, '$.kind'));
    SELECT RAISE(ABORT, 'claim transition rewrites acceptance')
    WHERE NEW.assignment_id IS NOT OLD.assignment_id
       OR NEW.recipient_lookup IS NOT OLD.recipient_lookup
       OR NEW.wire_seq IS NOT OLD.wire_seq
       OR NEW.message_id IS NOT OLD.message_id
       OR NEW.recipient IS NOT OLD.recipient
       OR NEW.audience IS NOT OLD.audience
       OR (({assignment_mode_new})) IS NOT (({assignment_mode_old}))
       OR NEW.resolver_version IS NOT OLD.resolver_version
       OR NEW.policy_version IS NOT OLD.policy_version
       OR NEW.accepted_at_ms IS NOT OLD.accepted_at_ms
       OR NEW.revision != OLD.revision + 1
       OR NEW.updated_at_ms < OLD.updated_at_ms;
    SELECT RAISE(ABORT, 'claim transition erases engagement facts')
    WHERE ((({assignment_verdict_old})) IS NOT NULL
           AND (({assignment_verdict_new})) IS NOT (({assignment_verdict_old})))
       OR ((json_extract(OLD.lifecycle, '$.decision.execution_id')) IS NOT NULL AND (json_extract(NEW.lifecycle, '$.decision.execution_id')) IS NOT (json_extract(OLD.lifecycle, '$.decision.execution_id')))
       OR ((json_extract(OLD.lifecycle, '$.decision.exact_target')) IS NOT NULL AND (json_extract(NEW.lifecycle, '$.decision.exact_target')) IS NOT (json_extract(OLD.lifecycle, '$.decision.exact_target')))
       OR ((json_extract(OLD.lifecycle, '$.decision.exact_target')) IS NULL AND (json_extract(NEW.lifecycle, '$.decision.exact_target')) IS NOT NULL
           AND (json_extract(NEW.lifecycle, '$.decision.execution_id')) IS NULL)
       OR ((json_extract(OLD.lifecycle, '$.decision.execution_id')) IS NULL AND (json_extract(NEW.lifecycle, '$.decision.execution_id')) IS NOT NULL
           AND (json_extract(NEW.lifecycle, '$.kind')) != 'engaged');
    SELECT RAISE(ABORT, 'claim disposition edge is not realizable')
    WHERE (json_extract(OLD.lifecycle, '$.kind')) != (json_extract(NEW.lifecycle, '$.kind')) AND NOT ({assignment_edges}
    );
    SELECT RAISE(ABORT, 'pre-engagement failure cannot invent execution')
    WHERE (json_extract(NEW.lifecycle, '$.kind')) = 'failed' AND (json_extract(OLD.lifecycle, '$.decision.execution_id')) IS NULL
      AND (json_extract(NEW.lifecycle, '$.decision.execution_id')) IS NOT NULL;
    SELECT RAISE(ABORT, 'post-engagement deferral cannot become pending')
    WHERE (json_extract(OLD.lifecycle, '$.kind')) = 'deferred' AND (json_extract(OLD.lifecycle, '$.decision.execution_id')) IS NOT NULL
      AND (json_extract(NEW.lifecycle, '$.kind')) IN ('triage_pending', 'full_pending');
END"""
            ),
            "wake_claim_delete_frozen": (
                """CREATE TRIGGER wake_claim_delete_frozen BEFORE DELETE ON wake_claims BEGIN
    SELECT RAISE(ABORT, 'wake claim cannot be deleted');
END"""
            ),
        }


@dataclass(frozen=True, slots=True)
class ExecutionAssignmentLink(CoordinatorTable, TypedTable, declared_name="execution_claims"):
    execution_id: str = dataclass_field(metadata={"sql": Column(primary_key=True)})
    assignment_id: str = dataclass_field(
        metadata={"wire_name": "claim_id", "sql": Column(unique=True)}
    )
    ordinal: int = dataclass_field(metadata={"sql": Column(primary_key=True, check="ordinal>=0")})
    unique = (("execution_id", "assignment_id"),)

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        require_nonempty(self.assignment_id, "claim_id")
        require_bounded(self.assignment_id, "claim_id", MAX_IDENTIFIER_CHARS)
        if self.ordinal < 0:
            raise ValueError("execution claim ordinal cannot be negative")

    @classmethod
    def references(cls):
        from agent_comms.coordination_tables.executions import ExecutionRecord

        return (
            ForeignKey(("execution_id",), ExecutionRecord, ("execution_id",), on_delete="RESTRICT"),
            ForeignKey(
                ("assignment_id", "execution_id"), WakeAssignment, ("assignment_id", "execution_id")
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "execution_claim_membership_insert": (
                """CREATE TRIGGER execution_claim_membership_insert BEFORE INSERT ON
    execution_claims
    WHEN NOT EXISTS (SELECT 1 FROM executions e WHERE e.execution_id =
    NEW.execution_id
      AND e.status IN ({unstarted_execution_names}))
     OR NEW.ordinal != (SELECT count(*) FROM execution_claims
                        WHERE execution_id = NEW.execution_id)
    BEGIN SELECT RAISE(ABORT,
    'execution claims require initial contiguous membership' ); END"""
            ),
            "execution_claim_target_insert": (
                """CREATE TRIGGER execution_claim_target_insert
    BEFORE INSERT ON execution_claims
    WHEN NOT EXISTS (
        SELECT 1 FROM executions e JOIN wake_claims c
          ON c.execution_id = e.execution_id
        WHERE e.execution_id = NEW.execution_id AND c.assignment_id = NEW.assignment_id
          AND c.exact_target = e.exact_target
          AND c.recipient_lookup = e.owner_lookup
    )
    BEGIN
        SELECT RAISE(ABORT, 'claim target does not match execution target');
    END"""
            ),
            "execution_claim_target_update": (
                """CREATE TRIGGER execution_claim_target_update
    BEFORE UPDATE ON execution_claims
    BEGIN
        SELECT RAISE(ABORT, 'execution claim relation is immutable');
    END"""
            ),
            "execution_claim_delete_frozen": (
                """CREATE TRIGGER execution_claim_delete_frozen BEFORE DELETE ON
    execution_claims BEGIN
        SELECT RAISE(ABORT, 'execution claim relation cannot be deleted'
        );
    END"""
            ),
        }
