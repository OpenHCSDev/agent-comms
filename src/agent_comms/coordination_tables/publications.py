"""Publication rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Annotated, ClassVar

from agent_comms.coordination_contracts import (
    MAX_IDENTIFIER_CHARS,
    MAX_PUBLICATION_PAYLOAD_BYTES,
    require_bounded,
    require_nonempty,
    validate_execution_id,
)
from agent_comms.coordination_errors import CoordinationError, IntegrityViolationError
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.field_codec import FieldCodec, MalformedRecord, WireName
from agent_comms.message_reference import MessageReference
from agent_comms.messages import Message
from agent_comms.response_acknowledgement import canonical_publication_key
from agent_comms.typed_table import (
    Column,
    ExactStorage,
    ForeignKey,
    Generated,
    GuardedInserts,
    ImmutableRows,
    TypedTable,
    ValidatedInserts,
)


@dataclass(frozen=True, slots=True)
class PublicationIntents(
    ValidatedInserts, GuardedInserts, ImmutableRows, CoordinatorTable, TypedTable
):
    """One obligation route's frozen response: the exact Message it publishes."""

    insert_condition = """NOT EXISTS (SELECT 1 FROM obligations
        WHERE execution_id = NEW.execution_id AND exact_target = NEW.exact_target
        AND state = 'pending')"""


    validator_function: ClassVar[str] = "coordination_validate_publication_intent"

    @classmethod
    def configure_connection(cls, connection) -> None:
        connection.create_function(
            cls.validator_function, sum(item.init for item in fields(cls)),
            cls.validate_sql, deterministic=True,
        )

    def matches_request(self, execution_id: str, message: Message) -> bool:
        return self.execution_id == execution_id and self.message == message

    def matches_publication(self, message: Message) -> bool:
        """Compare the frozen intent once against the Message-owned content snapshot."""
        return self.message == message.publication_snapshot

    def validate_acknowledgement(self, acknowledgement: PublicationEvidence) -> None:
        if acknowledgement.message_id != self.expected_message_id:
            raise IntegrityViolationError("acknowledgement envelope does not match frozen intent")

    execution_id: Annotated[
        str,
        Column(
            primary_key=True,
            storage=ExactStorage,
            check="""typeof(execution_id) = 'text' AND length(execution_id) BETWEEN 1 AND 256
             AND instr(execution_id, ':' ) = 0""",
        ),
    ]
    exact_target: Annotated[
        str,
        Column(
            primary_key=True,
            storage=ExactStorage,
            check="""
      typeof(exact_target) = 'text' AND length(exact_target) BETWEEN 1 AND 256""",
        ),
    ]
    message: Annotated[Message, Column(check="json_valid(message)")]
    publication_key: Annotated[
        str,
        Column(
            storage=ExactStorage,
            unique=True,
            check="""
      typeof(publication_key) = 'text' AND length(publication_key) BETWEEN 1 AND 256
    """,
        ),
    ]
    expected_message_id: Annotated[
        str,
        Column(
            storage=ExactStorage,
            unique=True,
            check="""
      typeof(expected_message_id) = 'text' AND length(expected_message_id) BETWEEN 1 AND
      256
    """,
        ),
    ]

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        require_nonempty(self.message.body, "payload")
        require_bounded(self.message.sender, "sender", MAX_IDENTIFIER_CHARS)
        if self.message.target != self.exact_target:
            raise IntegrityViolationError("publication route is not its message target")
        if len(self.message.body.encode("utf-8")) > MAX_PUBLICATION_PAYLOAD_BYTES:
            raise ValueError("publication payload exceeds byte limit")
        if self.publication_key != canonical_publication_key(self.execution_id, self.exact_target):
            raise IntegrityViolationError("publication key is not canonical")
        if self.message.message_id != self.expected_message_id:
            raise IntegrityViolationError("expected message id does not match Message authority")

    obligation_intent_required: ClassVar[Generated] = Generated(
        int,
        Column(generated="1", nullable=True),
    )

    @classmethod
    def references(cls):
        from agent_comms.coordination_tables.executions import ExecutionRecord
        from agent_comms.coordination_tables.responses import ResponseObligation

        return (
            ForeignKey(("execution_id",), ExecutionRecord, ("execution_id",), on_delete="RESTRICT"),
            ForeignKey(
                ("execution_id", "exact_target", "obligation_intent_required"),
                ResponseObligation,
                ("execution_id", "exact_target", "intent_settled"),
                deferred=True,
            ),
        )

    @classmethod
    def validate_sql(cls, *values: object) -> int:
        """SQLite positional input follows the declared columns; decode and validate once."""
        try:
            FieldCodec.construct(cls, {
                item.name: item.decode(value)
                for item, value in zip(
                    (item for item in cls._fields() if item.init), values, strict=True
                )
            })
        except (MalformedRecord, CoordinationError):
            return 0  # The trigger refuses an invalid envelope; other errors are defects.
        return 1


@dataclass(frozen=True, kw_only=True)
class PublicationEvidence(
    GuardedInserts,
    ImmutableRows,
    CoordinatorTable,
    TypedTable,
    declared_name="publication_receipts",
):
    insert_condition = """(EXISTS (SELECT 1 FROM executions
        WHERE execution_id = NEW.execution_id AND status = 'failed'))
        OR (NEW.message_id != (SELECT expected_message_id FROM publication_intents
        WHERE execution_id = NEW.execution_id AND exact_target = NEW.exact_target))
        OR (NOT EXISTS (SELECT 1 FROM obligations WHERE execution_id = NEW.execution_id
        AND exact_target = NEW.exact_target AND state = 'publishing'))"""

    @property
    def reference(self) -> MessageReference:
        return MessageReference(self.seq, self.message_id)

    execution_id: Annotated[
        str,
        Column(
            primary_key=True,
            storage=ExactStorage,
            check=(
                "typeof(execution_id) = 'text' AND length(execution_id) BETWEEN 1"
                " AND 256\n"
                "             AND instr(execution_id, ':') = 0"
            ),
        ),
    ]
    exact_target: Annotated[
        str,
        Column(
            primary_key=True, storage=ExactStorage, check="length(exact_target) BETWEEN 1 AND 256"
        ),
    ]
    seq: Annotated[int, Column(unique=True, check="seq > 0")]
    message_id: Annotated[
        str,
        Column(
            storage=ExactStorage,
            unique=True,
            check=("\n      typeof(message_id) = 'text' AND length(message_id) BETWEEN 1 AND 256"),
        ),
    ]
    received_at_ms: Annotated[int, Column(check="received_at_ms >= 0")]
    obligation_publication_required: ClassVar[Generated] = Generated(
        Annotated[int, WireName("obligation_receipt_required")],
        Column(record_key=True, generated="1", nullable=True),
    )

    @classmethod
    def references(cls):
        from agent_comms.coordination_tables.responses import ResponseObligation

        return (
            ForeignKey(
                ("execution_id", "exact_target"),
                PublicationIntents,
                ("execution_id", "exact_target"),
            ),
            ForeignKey(
                ("execution_id", "exact_target", "obligation_publication_required"),
                ResponseObligation,
                ("execution_id", "exact_target", "publication_settled"),
                deferred=True,
            ),
        )
