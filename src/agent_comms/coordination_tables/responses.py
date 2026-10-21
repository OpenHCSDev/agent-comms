"""Response rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, ClassVar

from agent_comms.coordination_contracts import (
    MAX_IDENTIFIER_CHARS,
    require_bounded,
    require_nonempty,
    validate_execution_id,
)
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.coordination_tables.executions import ExecutionReference, RetryPartition
from agent_comms.field_codec import SnapshotExclude, WireName, projected
from agent_comms.obligation_states import ResponseState
from agent_comms.reason_codes import ReasonCode
from agent_comms.typed_table import (
    Column,
    ColumnGuard,
    FrozenColumns,
    Generated,
    GuardedColumns,
    GuardedInserts,
    GuardedUpdateMembership,
    IncrementingColumn,
    LifecycleChanges,
    LifecycleTransitions,
    MonotoneColumns,
    NoDelete,
    NonDecreasing,
    TextStorage,
    TypedTable,
)


@dataclass(frozen=True, slots=True)
class ResponseObligation(
    LifecycleTransitions,
    LifecycleChanges,
    IncrementingColumn,
    MonotoneColumns,
    ExecutionReference,
    RetryPartition,
    GuardedUpdateMembership,
    FrozenColumns,
    GuardedColumns,
    GuardedInserts,
    NoDelete,
    CoordinatorTable,
    TypedTable,
    declared_name="obligations",
):
    """One original reply-route obligation within an execution."""

    column_orders = (NonDecreasing(("updated_at_ms",)),)
    immutable_columns = ("execution_id", "exact_target", "created_at_ms")
    update_membership_condition = """(json_extract(NEW.lifecycle, '$.kind')
        IN ({required_intent_response_names})) AND ((NOT EXISTS (SELECT 1 FROM publication_intents
        WHERE execution_id = NEW.execution_id AND exact_target = NEW.exact_target))
        OR (json_extract(NEW.lifecycle, '$.kind') = 'published' AND NOT EXISTS (
        SELECT 1 FROM publication_receipts WHERE execution_id = NEW.execution_id
        AND exact_target = NEW.exact_target
        AND message_id = json_extract(NEW.lifecycle, '$.message_id')
        AND seq = json_extract(NEW.lifecycle, '$.seq'))))"""
    insert_condition = """(NOT EXISTS (SELECT 1 FROM executions e
        JOIN wake_claims c ON c.execution_id=e.execution_id WHERE e.execution_id=NEW.execution_id
        AND e.origin='wire' AND c.exact_target=NEW.exact_target))
        OR (json_extract(NEW.lifecycle, '$.kind') IN ({required_intent_response_names}))"""
    column_guards = (
        ColumnGuard(("lifecycle",), """(EXISTS (SELECT 1 FROM publication_receipts
        WHERE execution_id = OLD.execution_id AND exact_target = OLD.exact_target)
        AND json_extract(NEW.lifecycle, '$.kind') != 'published') OR (EXISTS (
        SELECT 1 FROM publication_intents WHERE execution_id = OLD.execution_id
        AND exact_target = OLD.exact_target) AND json_extract(NEW.lifecycle, '$.kind')
        NOT IN ({intent_response_names}))"""),
        ColumnGuard(("exact_target",), """(NOT EXISTS (SELECT 1 FROM executions e
        JOIN wake_claims c ON c.execution_id=e.execution_id WHERE e.execution_id=NEW.execution_id
        AND e.origin='wire' AND c.exact_target=NEW.exact_target)) OR (EXISTS (
        SELECT 1 FROM publication_intents WHERE execution_id = OLD.execution_id
        AND exact_target = OLD.exact_target))"""),
    )


    @staticmethod
    def response_record(records, exact_target):
        return next((row for row in records if row.exact_target == exact_target), None)

    execution_id: Annotated[str, SnapshotExclude(), Column(primary_key=True)]
    exact_target: Annotated[
        str, Column(primary_key=True, check="length(exact_target) BETWEEN 1 AND 256")
    ]
    lifecycle: Annotated[ResponseState, SnapshotExclude()]
    reason_code: Annotated[
        ReasonCode,
        Column(storage=TextStorage, nullable=True,
               check="reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 64"),
    ]
    created_at_ms: Annotated[int, Column(check="created_at_ms >= 0")]
    updated_at_ms: Annotated[int, Column(check="updated_at_ms >= created_at_ms")]
    revision: Annotated[int, Column(check="revision > 0")]

    @projected(view="snapshot", name="state")
    def snapshot_state(self):
        return type(self.lifecycle)

    @projected(view="snapshot", name="receipt_message_id")
    def snapshot_receipt_message_id(self):
        return self.lifecycle.published_message_id

    @projected(view="snapshot", name="receipt_seq")
    def snapshot_receipt_seq(self):
        return self.lifecycle.publication_seq

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        require_nonempty(self.exact_target, "exact_target")
        require_bounded(self.exact_target, "exact_target", MAX_IDENTIFIER_CHARS)
        if self.revision <= 0:
            raise ValueError("revision must be positive")
        if self.created_at_ms < 0 or self.updated_at_ms < self.created_at_ms:
            raise ValueError("obligation timestamps are inconsistent")

    state: ClassVar[Generated] = Generated(
        str,
        Column(generated="json_extract(lifecycle, '$.kind')", check="state IN ({response_names})"),
    )
    published_message_id: ClassVar[Generated] = Generated(
        Annotated[str, WireName("receipt_message_id")],
        Column(
            record_key=True,
            generated="json_extract(lifecycle, '$.message_id')",
            check=(
                "\n"
                "        receipt_message_id IS NULL OR length(receipt_message_id)"
                " BETWEEN 1 AND 256\n"
                "    "
            ),
            nullable=True,
        ),
    )
    publication_seq: ClassVar[Generated] = Generated(
        Annotated[int, WireName("receipt_seq")],
        Column(
            record_key=True,
            generated="json_extract(lifecycle, '$.seq')",
            check="receipt_seq IS NULL OR receipt_seq > 0",
            nullable=True,
        ),
    )
    success_terminal: ClassVar[Generated] = Generated(
        int,
        Column(
            generated="CASE WHEN state IN ({successful_response_names}) THEN 1 ELSE 0 END",
            nullable=True,
        ),
    )
    retryable: ClassVar[Generated] = Generated(
        int,
        Column(
            generated="CASE WHEN state IN ({retryable_response_names}) THEN 1 ELSE 0 END",
            nullable=True,
        ),
    )
    intent_settled: ClassVar[Generated] = Generated(
        int,
        Column(
            generated="CASE WHEN state IN ({intent_response_names}) THEN 1 ELSE 0 END",
            nullable=True,
        ),
    )
    publication_settled: ClassVar[Generated] = Generated(
        Annotated[int, WireName("receipt_settled")],
        Column(
            record_key=True,
            generated="CASE WHEN state = 'published' THEN 1 ELSE 0 END",
            nullable=True,
        ),
    )
    checks = (
        "(receipt_message_id IS NULL) = (receipt_seq IS NULL)",
        (
            "\n"
            "        (state = 'published' AND receipt_message_id IS NOT NULL)"
            "\n"
            "        OR (state != 'published' AND receipt_message_id IS NULL)"
            "\n"
            "    "
        ),
    )
    unique = (
        ("execution_id", "exact_target", "success_terminal"),
        ("execution_id", "exact_target", "retryable"),
        ("execution_id", "exact_target", "intent_settled"),
        ("execution_id", "exact_target", "publication_settled"),
    )
