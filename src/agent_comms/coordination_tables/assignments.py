"""Assignment rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Annotated, ClassVar

from agent_comms.assignment_states import AssignmentState, EngagedAssignment
from agent_comms.coordination_contracts import (
    MAX_IDENTIFIER_CHARS,
    POLICY_VERSION,
    RESOLVER_VERSION,
    require_bounded,
    require_nonempty,
    require_optional_nonempty,
    validate_execution_id,
)
from agent_comms.coordination_schema import CoordinatorRow
from agent_comms.envelope_claim_transitions import AssignmentBinding
from agent_comms.field_codec import SnapshotExclude, WireName, projected
from agent_comms.message_reference import MessageReference
from agent_comms.typed_table import (
    Column,
    FirstValueState,
    ForeignKey,
    FrozenColumn,
    Generated,
    GuardedInserts,
    ImmutableRows,
    IncrementingColumn,
    Index,
    LifecycleChanges,
    LifecycleTransitions,
    MonotoneColumns,
    NoDelete,
    NonDecreasing,
    RequiredColumn,
    WriteOnceColumns,
)
from agent_comms.wake_policy import MessageAudience


@dataclass(frozen=True, slots=True)
class WakeAssignment(
    WriteOnceColumns,
    FirstValueState,
    RequiredColumn,
    LifecycleTransitions,
    LifecycleChanges,
    IncrementingColumn,
    MonotoneColumns,
    NoDelete,
    CoordinatorRow,
    declared_name="wake_claims",
):
    write_once_columns = ('triage_verdict', 'execution_id', 'exact_target')
    initial_value_column = 'execution_id'
    initial_value_state = EngagedAssignment
    dependent_column = 'exact_target'
    required_column = 'execution_id'

    column_orders = (NonDecreasing(('updated_at_ms',)),)

    @property
    def recipient_identity(self):
        from agent_comms.audience_manifest import FrozenRecipient

        return FrozenRecipient(self.recipient_lookup, self.recipient)

    def require_selected_source(self, initial, owner) -> None:
        from agent_comms.audience_manifest import FrozenRecipient
        from agent_comms.coordination_errors import IdentityConflict

        if self.recipient_identity != FrozenRecipient(
            owner.incarnation.lookup, owner.name
        ):
            raise IdentityConflict("wake frame does not match a committed recipient")
        self.require_committed_source(initial)

    def require_committed_source(self, initial) -> None:
        from agent_comms.coordination_errors import IdentityConflict
        from agent_comms.wake_policy import WakeDecision

        if self.source != MessageReference(initial.message.seq, initial.message.message_id):
            raise IdentityConflict("wake frame does not match a committed source")
        expected = WakeDecision(self.recipient_lookup, self.audience, self.lifecycle.mode)
        selected = sum(
            recipient == self.recipient_identity and decision == expected
            for recipient, decision in zip(
                initial.audience.recipients, initial.decisions, strict=True
            )
        )
        if selected != 1:
            raise IdentityConflict("wake frame requires one selected N/K recipient")

    def require_initial_decision(self) -> None:
        initial = replace(
            self,
            lifecycle=self.lifecycle.mode.initial_state()(),
            updated_at_ms=self.accepted_at_ms,
            revision=1,
        )
        if (
            self != initial
            or self.resolver_version != RESOLVER_VERSION
            or self.policy_version != POLICY_VERSION
        ):
            from agent_comms.coordination_errors import IdentityConflict

            raise IdentityConflict("claim acceptance requires initial frozen decision")

    def require_same_acceptance(self, candidate: WakeAssignment) -> None:
        # Only lifecycle progress may differ from the original accepted row.
        progress = replace(
            candidate,
            lifecycle=self.lifecycle,
            updated_at_ms=self.updated_at_ms,
            revision=self.revision,
        )
        if self != progress or self.lifecycle.mode != candidate.lifecycle.mode:
            from agent_comms.coordination_errors import IdentityConflict

            raise IdentityConflict("accepted claim identity conflicts")

    @property
    def source(self) -> MessageReference:
        return MessageReference(self.wire_seq, self.message_id)

    def require_engaged_binding(self) -> AssignmentBinding:
        engagement = self.lifecycle.require_engagement()
        return AssignmentBinding(
            self.assignment_id,
            self.revision,
            self.recipient_lookup,
            self.source,
            engagement.execution_id,
        )

    assignment_id: Annotated[
        str,
        WireName("claim_id"),
        FrozenColumn(primary_key=True, check="length(assignment_id) BETWEEN 1 AND 256"),
    ]
    recipient: Annotated[str, FrozenColumn(check="length(recipient) BETWEEN 1 AND 256")]
    recipient_lookup: Annotated[str, FrozenColumn()]
    wire_seq: Annotated[int, FrozenColumn(check="wire_seq > 0")]
    message_id: Annotated[str, FrozenColumn(check="length(message_id) BETWEEN 1 AND 256")]
    audience: Annotated[MessageAudience, FrozenColumn()]
    lifecycle: Annotated[AssignmentState, SnapshotExclude()]
    accepted_at_ms: Annotated[int, FrozenColumn(check="accepted_at_ms >= 0")]
    updated_at_ms: Annotated[int, Column(check="updated_at_ms >= accepted_at_ms")]
    revision: Annotated[int, Column(check="revision > 0")] = 1
    resolver_version: Annotated[
        str, FrozenColumn(check="length(resolver_version) BETWEEN 1 AND 256")
    ] = RESOLVER_VERSION
    policy_version: Annotated[
        str, FrozenColumn(check="length(policy_version) BETWEEN 1 AND 256")
    ] = POLICY_VERSION

    @projected(view="snapshot", name="disposition")
    def snapshot_disposition(self):
        return type(self.lifecycle)

    @projected(view="snapshot", name="wake_mode")
    def snapshot_wake_mode(self):
        return type(self.lifecycle.mode)

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

    exact_target: ClassVar[Generated] = Generated(
        str,
        Column(
            generated="json_extract(lifecycle, '$.decision.exact_target')",
            check="exact_target IS NULL OR length(exact_target) BETWEEN 1 AND 256",
            nullable=True,
        ),
    )
    wake_mode: ClassVar[Generated] = Generated(
        str,
        FrozenColumn(generated="({assignment_mode})", check="wake_mode IN ({wake_names})"),
    )
    triage_verdict: ClassVar[Generated] = Generated(
        str,
        Column(
            generated="({assignment_verdict})",
            check="triage_verdict IS NULL OR triage_verdict IN ('ignore', 'engage')",
            nullable=True,
        ),
    )
    disposition: ClassVar[Generated] = Generated(
        str,
        Column(
            generated="json_extract(lifecycle, '$.kind')",
            check="disposition IN ({assignment_names})",
        ),
    )
    execution_id: ClassVar[Generated] = Generated(
        str,
        Column(
            generated="json_extract(lifecycle, '$.decision.execution_id')",
            nullable=True,
        ),
    )
    claim_status_kind: ClassVar[Generated] = Generated(
        str,
        Column(generated="CASE WHEN execution_id IS NOT NULL THEN disposition END", nullable=True),
    )
    checks = ("COALESCE(({assignment_binding}), 0)",)
    unique = (("recipient_lookup", "wire_seq"), ("assignment_id", "execution_id"))
    indexes = (Index(("execution_id",), unique=False, where=None),)

    @classmethod
    def references(cls):
        from agent_comms.coordination_tables.executions import ExecutionRecord
        from agent_comms.coordination_tables.participants import Participants
        from agent_comms.coordination_tables.responses import ResponseObligation

        return (
            ForeignKey(("recipient_lookup",), Participants, ("participant_lookup",)),
            ForeignKey(("execution_id",), ExecutionRecord, ("execution_id",)),
            ForeignKey(
                ("execution_id", "exact_target"), ResponseObligation,
                ("execution_id", "exact_target"), deferred=True,
            ),
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

@dataclass(frozen=True, slots=True)
class ExecutionAssignmentLink(
    GuardedInserts, ImmutableRows, CoordinatorRow, declared_name="execution_claims"
):

    insert_condition = """(NOT EXISTS (SELECT 1 FROM executions e
        WHERE e.execution_id = NEW.execution_id AND e.status IN ({unstarted_execution_names}))
        OR NEW.ordinal != (SELECT count(*) FROM execution_claims
        WHERE execution_id = NEW.execution_id)) OR (NOT EXISTS (
        SELECT 1 FROM executions e JOIN wake_claims c ON c.execution_id = e.execution_id
        WHERE e.execution_id = NEW.execution_id AND c.assignment_id = NEW.assignment_id
        AND c.exact_target IS NOT NULL AND c.recipient_lookup = e.owner_lookup ))"""

    execution_id: Annotated[str, Column(primary_key=True)]
    assignment_id: Annotated[str, WireName("claim_id"), Column(unique=True)]
    ordinal: Annotated[int, Column(primary_key=True, check="ordinal>=0")]
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
