"""Response rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field

from agent_comms.coordination_contracts import (
    MAX_IDENTIFIER_CHARS,
    MAX_REASON_CODE_CHARS,
    require_bounded,
    require_nonempty,
    require_optional_nonempty,
    validate_execution_id,
)
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.field_codec import projected
from agent_comms.obligation_states import ResponseState
from agent_comms.typed_table import (
    Column,
    ForeignKey,
    TypedTable,
)


@dataclass(frozen=True, slots=True)
class ResponseObligation(CoordinatorTable, TypedTable, declared_name="obligations"):
    """One original reply-route obligation within an execution."""

    @staticmethod
    def response_record(records, exact_target):
        return next((row for row in records if row.exact_target == exact_target), None)

    execution_id: str = dataclass_field(
        metadata={"snapshot_exclude": True, "sql": Column(primary_key=True)}
    )
    exact_target: str = dataclass_field(
        metadata={"sql": Column(primary_key=True, check="length(exact_target) BETWEEN 1 AND 256")}
    )
    lifecycle: ResponseState = dataclass_field(metadata={"snapshot_exclude": True})
    reason_code: str | None = dataclass_field(
        metadata={
            "sql": Column(check="reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 64")
        }
    )
    created_at_ms: int = dataclass_field(metadata={"sql": Column(check="created_at_ms >= 0")})
    updated_at_ms: int = dataclass_field(
        metadata={"sql": Column(check="updated_at_ms >= created_at_ms")}
    )
    revision: int = dataclass_field(metadata={"sql": Column(check="revision > 0")})

    @projected(view="snapshot", name="state")
    def snapshot_state(self):
        return type(self.lifecycle)

    @projected(view="snapshot", name="receipt_message_id")
    def snapshot_receipt_message_id(self):
        return self.lifecycle.receipt_message_id

    @projected(view="snapshot", name="receipt_seq")
    def snapshot_receipt_seq(self):
        return self.lifecycle.receipt_seq

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        require_nonempty(self.exact_target, "exact_target")
        require_bounded(self.exact_target, "exact_target", MAX_IDENTIFIER_CHARS)
        require_optional_nonempty(self.reason_code, "reason_code", MAX_REASON_CODE_CHARS)
        require_optional_nonempty(
            self.lifecycle.receipt_message_id, "receipt_message_id", MAX_IDENTIFIER_CHARS
        )
        if self.revision <= 0:
            raise ValueError("revision must be positive")
        if self.created_at_ms < 0 or self.updated_at_ms < self.created_at_ms:
            raise ValueError("obligation timestamps are inconsistent")

    state: str = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.kind')", check="state IN ({response_names})"
            ),
        },
    )
    receipt_message_id: str | None = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.message_id')",
                check=(
                    "\n"
                    "        receipt_message_id IS NULL OR length(receipt_message_id)"
                    " BETWEEN 1 AND 256\n"
                    "    "
                ),
            ),
        },
    )
    receipt_seq: int | None = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.seq')",
                check="receipt_seq IS NULL OR receipt_seq > 0",
            ),
        },
    )
    success_terminal: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="CASE WHEN state IN ({successful_response_names}) THEN 1 ELSE 0 END"
            ),
        },
    )
    retryable: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="CASE WHEN state IN ({retryable_response_names}) THEN 1 ELSE 0 END"
            ),
        },
    )
    intent_settled: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="CASE WHEN state IN ({intent_response_names}) THEN 1 ELSE 0 END"
            ),
        },
    )
    receipt_settled: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(generated="CASE WHEN state = 'published' THEN 1 ELSE 0 END"),
        },
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
        ("execution_id", "exact_target", "receipt_settled"),
    )

    @classmethod
    def references(cls):
        from agent_comms.coordination_tables.executions import ExecutionRecord

        return (
            ForeignKey(("execution_id",), ExecutionRecord, ("execution_id",), on_delete="RESTRICT"),
        )

    @classmethod
    def triggers(cls):
        return {**cls._retry_triggers(), **cls._lifecycle_triggers(), **cls._publication_triggers()}

    @classmethod
    def _retry_triggers(cls):
        return {
            "failed_retry_partition_obligation_insert": """CREATE TRIGGER failed_retry_partition_obligation_insert AFTER INSERT
ON obligations
WHEN EXISTS (SELECT 1 FROM retry_disposition_basis b JOIN executions e
             ON e.execution_id = b.execution_id
             WHERE b.execution_id = NEW.execution_id AND b.authorized
             = 1
               AND e.status = 'failed' AND e.current_attempt_ordinal
               IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'authorized retry cannot settle failed' );
END""",
            "failed_retry_partition_obligation_update": """CREATE TRIGGER failed_retry_partition_obligation_update AFTER UPDATE
ON obligations
WHEN EXISTS (SELECT 1 FROM retry_disposition_basis b JOIN executions e
             ON e.execution_id = b.execution_id
             WHERE b.execution_id = NEW.execution_id AND b.authorized
             = 1
               AND e.status = 'failed' AND e.current_attempt_ordinal
               IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'authorized retry cannot settle failed' );
END""",
        }

    @classmethod
    def _lifecycle_triggers(cls):
        return {
            "obligation_same_state_frozen": """CREATE TRIGGER obligation_same_state_frozen BEFORE UPDATE ON obligations
WHEN json_extract(NEW.lifecycle, '$.kind') = json_extract(OLD.lifecycle, '$.kind')
BEGIN SELECT RAISE(ABORT, 'obligation disposition must change'); END""",
            "obligation_declared_edge": """CREATE TRIGGER obligation_declared_edge
BEFORE UPDATE OF lifecycle ON obligations
WHEN json_extract(OLD.lifecycle, '$.kind') != json_extract(NEW.lifecycle, '$.kind') AND NOT ({obligation_edges}
)
BEGIN
    SELECT RAISE(ABORT, 'obligation state transition is not declared');
END""",
            "obligation_frozen_facts": """CREATE TRIGGER obligation_frozen_facts
BEFORE UPDATE ON obligations
WHEN NEW.execution_id IS NOT OLD.execution_id
 OR NEW.exact_target IS NOT OLD.exact_target
 OR NEW.created_at_ms != OLD.created_at_ms
 OR NEW.revision != OLD.revision + 1
 OR NEW.updated_at_ms < OLD.updated_at_ms
BEGIN
    SELECT RAISE(ABORT, 'obligation transition rewrites frozen facts');
END""",
            "obligation_target_matches_execution_insert": """CREATE TRIGGER obligation_target_matches_execution_insert
BEFORE INSERT ON obligations
WHEN NOT EXISTS (
    SELECT 1 FROM executions e JOIN wake_claims c ON c.execution_id=e.execution_id
    WHERE e.execution_id=NEW.execution_id AND e.origin='wire' AND c.exact_target=NEW.exact_target
)
BEGIN
    SELECT RAISE(ABORT, 'obligation target does not match wire execution');
END""",
            "obligation_target_matches_execution_update": """CREATE TRIGGER obligation_target_matches_execution_update
BEFORE UPDATE OF exact_target ON obligations
WHEN NOT EXISTS (
    SELECT 1 FROM executions e JOIN wake_claims c ON c.execution_id=e.execution_id
    WHERE e.execution_id=NEW.execution_id AND e.origin='wire' AND c.exact_target=NEW.exact_target
)
BEGIN
    SELECT RAISE(ABORT, 'obligation target does not match wire execution');
END""",
            "obligation_delete_frozen": """CREATE TRIGGER obligation_delete_frozen BEFORE DELETE ON obligations BEGIN
    SELECT RAISE(ABORT, 'obligation cannot be deleted');
END""",
        }

    @classmethod
    def _publication_triggers(cls):
        return {
            "published_obligation_receipt_is_frozen": """CREATE TRIGGER published_obligation_receipt_is_frozen
BEFORE UPDATE OF lifecycle ON obligations
WHEN json_extract(OLD.lifecycle, '$.kind') = 'published' AND (
    json_extract(NEW.lifecycle, '$.message_id') IS NOT json_extract(OLD.lifecycle, '$.message_id')
    OR json_extract(NEW.lifecycle, '$.seq') IS NOT json_extract(OLD.lifecycle, '$.seq')
)
BEGIN
    SELECT RAISE(ABORT, 'published obligation receipt is frozen');
END""",
            "obligation_publication_transition": """CREATE TRIGGER obligation_publication_transition
BEFORE UPDATE ON obligations
WHEN json_extract(NEW.lifecycle, '$.kind') IN ({required_intent_response_names})
BEGIN
    SELECT RAISE(ABORT, 'publishing obligation requires intent')
    WHERE NOT EXISTS (
        SELECT 1 FROM publication_intents
        WHERE execution_id = NEW.execution_id AND exact_target = NEW.exact_target
    );
    SELECT RAISE(ABORT, 'published obligation requires matching receipt')
    WHERE json_extract(NEW.lifecycle, '$.kind') = 'published' AND NOT EXISTS (
        SELECT 1 FROM publication_receipts
        WHERE execution_id = NEW.execution_id AND exact_target = NEW.exact_target
          AND message_id = json_extract(NEW.lifecycle, '$.message_id') AND seq = json_extract(NEW.lifecycle, '$.seq')
    );
END""",
            "obligation_publication_insert": """CREATE TRIGGER obligation_publication_insert
BEFORE INSERT ON obligations
WHEN json_extract(NEW.lifecycle, '$.kind') IN ({required_intent_response_names})
BEGIN
    SELECT RAISE(ABORT, 'publication obligation starts pending');
END""",
            "obligation_receipt_requires_published": """CREATE TRIGGER obligation_receipt_requires_published
BEFORE UPDATE OF lifecycle ON obligations
WHEN EXISTS (SELECT 1 FROM publication_receipts WHERE execution_id = OLD.execution_id AND exact_target = OLD.exact_target)
 AND json_extract(NEW.lifecycle, '$.kind') != 'published'
BEGIN
    SELECT RAISE(ABORT, 'frozen receipt requires published obligation');
END""",
            "obligation_state_with_intent": """CREATE TRIGGER obligation_state_with_intent
BEFORE UPDATE OF lifecycle ON obligations
WHEN EXISTS (SELECT 1 FROM publication_intents WHERE execution_id = OLD.execution_id AND exact_target = OLD.exact_target)
  AND json_extract(NEW.lifecycle, '$.kind') NOT IN ({intent_response_names})
BEGIN
    SELECT RAISE(ABORT, 'frozen intent cannot return to pending obligation');
END""",
            "obligation_target_frozen": """CREATE TRIGGER obligation_target_frozen
BEFORE UPDATE OF exact_target ON obligations
WHEN EXISTS (SELECT 1 FROM publication_intents WHERE execution_id = OLD.execution_id AND exact_target = OLD.exact_target)
BEGIN
    SELECT RAISE(ABORT, 'publication target is frozen');
END""",
        }
