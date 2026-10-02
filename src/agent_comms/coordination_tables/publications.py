"""Publication rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, fields
from dataclasses import field as dataclass_field

from agent_comms.coordination_contracts import (
    MAX_IDENTIFIER_CHARS,
    MAX_PUBLICATION_PAYLOAD_BYTES,
    require_bounded,
    require_nonempty,
    validate_execution_id,
)
from agent_comms.coordination_errors import IntegrityViolationError
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.field_codec import projected
from agent_comms.message_reference import MessageReference
from agent_comms.messages import Message, MessageType
from agent_comms.typed_table import (
    Column,
    ExactStorage,
    ForeignKey,
    TypedRow,
    TypedTable,
)


def canonical_publication_key(execution_id: str, exact_target: str) -> str:
    """Return the sole deterministic publication identity for an obligation route."""
    validate_execution_id(execution_id)
    require_nonempty(exact_target, "exact_target")
    require_bounded(exact_target, "exact_target", MAX_IDENTIFIER_CHARS)
    key = f"publication:v1:{execution_id}:{exact_target}"
    require_bounded(key, "publication_key", MAX_IDENTIFIER_CHARS)
    return key


@dataclass(frozen=True, slots=True)
class PublicationIntents(CoordinatorTable, TypedTable):
    def matches_request(self, execution_id: str, message: Message) -> bool:
        return self.execution_id == execution_id and self.expected_message == message

    def matches_publication(self, message: Message) -> bool:
        """Compare the frozen intent once against the Message-owned content snapshot."""
        return self.expected_message == message.publication_snapshot

    def validate_receipt(self, receipt: PublicationReceipt) -> None:
        expected = (
            self.execution_id, self.publication_key, self.expected_message_id,
            self.sender, self.exact_target, self.message_type, self.notice,
            self.timestamp, self.payload_digest,
        )
        observed = (
            receipt.execution_id, receipt.publication_key, receipt.message_id,
            receipt.sender, receipt.exact_target, receipt.message_type, receipt.notice,
            receipt.timestamp, receipt.payload_digest,
        )
        if observed != expected:
            raise IntegrityViolationError("receipt envelope does not match frozen intent")

    execution_id: str = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                primary_key=True,
                storage=ExactStorage,
                check="""typeof(execution_id) = 'text' AND length(execution_id) BETWEEN 1 AND 256
             AND instr(execution_id, ':' ) = 0""",
            ),
        }
    )
    sender: str = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                storage=ExactStorage,
                check="typeof(sender) = 'text' AND length(sender) BETWEEN 1 AND 256",
            ),
        }
    )
    exact_target: str = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                primary_key=True, storage=ExactStorage,
                check="""
      typeof(exact_target) = 'text' AND length(exact_target) BETWEEN 1 AND 256""",
            ),
        }
    )
    message_type: MessageType = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(storage=ExactStorage, check="typeof(message_type) = 'text'"),
        }
    )
    notice: bool = dataclass_field(metadata={"snapshot_exclude": True, "sql": Column()})
    timestamp: float = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(check="timestamp >= 0 AND timestamp < 1.0e999"),
        }
    )
    payload: str = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                storage=ExactStorage,
                check="""
        typeof(payload) = 'text' AND length(CAST(payload AS BLOB)) BETWEEN 1 AND 120000
    """,
            ),
        }
    )
    payload_digest: str = dataclass_field(
        metadata={
            "sql": Column(
                storage=ExactStorage,
                check="""
      typeof(payload_digest) = 'text' AND length(payload_digest) BETWEEN 1 AND 256""",
            )
        }
    )
    publication_key: str = dataclass_field(
        metadata={
            "sql": Column(
                storage=ExactStorage,
                unique=True,
                check="""
      typeof(publication_key) = 'text' AND length(publication_key) BETWEEN 1 AND 256
    """,
            )
        }
    )
    expected_message_id: str = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                storage=ExactStorage,
                unique=True,
                check="""
      typeof(expected_message_id) = 'text' AND length(expected_message_id) BETWEEN 1 AND
      256
    """,
            ),
        }
    )

    def __post_init__(self) -> None:
        for field, value in (
            ("execution_id", self.execution_id),
            ("sender", self.sender),
            ("exact_target", self.exact_target),
            ("payload", self.payload),
            ("payload_digest", self.payload_digest),
            ("publication_key", self.publication_key),
            ("expected_message_id", self.expected_message_id),
        ):
            require_nonempty(value, field)
        validate_execution_id(self.execution_id)
        for field, value in (
            ("sender", self.sender),
            ("payload_digest", self.payload_digest),
            ("expected_message_id", self.expected_message_id),
        ):
            require_bounded(value, field, MAX_IDENTIFIER_CHARS)
        object.__setattr__(self, "message_type", MessageType(self.message_type))
        timestamp = float(self.timestamp)
        object.__setattr__(self, "timestamp", 0.0 if timestamp == 0.0 else timestamp)
        if not math.isfinite(self.timestamp) or self.timestamp < 0:
            raise ValueError("publication timestamp must be finite and non-negative")
        object.__setattr__(self, "notice", bool(self.notice))
        if len(self.payload.encode("utf-8")) > MAX_PUBLICATION_PAYLOAD_BYTES:
            raise ValueError("publication payload exceeds byte limit")
        if hashlib.sha256(self.payload.encode()).hexdigest() != self.payload_digest:
            raise IntegrityViolationError("publication payload digest does not match")
        if self.publication_key != canonical_publication_key(self.execution_id, self.exact_target):
            raise IntegrityViolationError("publication key is not canonical")
        if self.expected_message.message_id != self.expected_message_id:
            raise IntegrityViolationError("expected message id does not match Message authority")

    @projected(view="snapshot")
    def payload_utf8_bytes(self):
        return len(self.payload.encode("utf-8"))

    @property
    def expected_message(self) -> Message:
        """Reconstruct through the existing message identity authority."""
        return Message(
            sender=self.sender,
            target=self.exact_target,
            body=self.payload,
            type=self.message_type,
            timestamp=self.timestamp,
            notice=self.notice,
        )

    obligation_intent_required: int | None = dataclass_field(
        init=False, compare=False, metadata={"sql": Column(generated="1")}
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
    def triggers(cls):
        return {
            "publication_intent_envelope_authority": (
                f"""CREATE TRIGGER publication_intent_envelope_authority BEFORE INSERT ON
publication_intents
WHEN coordination_validate_publication_intent(
  {", ".join("NEW." + field.name for field in fields(cls) if field.init)}) != 1
BEGIN SELECT RAISE(ABORT, 'publication intent envelope is invalid' );
END"""
            ),
            "publication_intent_requires_obligation": (
                """CREATE TRIGGER publication_intent_requires_obligation
BEFORE INSERT ON publication_intents
WHEN NOT EXISTS (
    SELECT 1 FROM obligations WHERE execution_id = NEW.execution_id
      AND exact_target = NEW.exact_target AND state = 'pending'
)
BEGIN
    SELECT RAISE(ABORT, 'publication intent requires matching pending obligation');
END"""
            ),
            "publication_intent_update_frozen": (
                """CREATE TRIGGER publication_intent_update_frozen
BEFORE UPDATE ON publication_intents
BEGIN
    SELECT RAISE(ABORT, 'publication intent is frozen');
END"""
            ),
            "publication_intent_delete_frozen": (
                """CREATE TRIGGER publication_intent_delete_frozen
BEFORE DELETE ON publication_intents
BEGIN
    SELECT RAISE(ABORT, 'publication intent is frozen');
END"""
            ),
        }

    @classmethod
    def validate_sql(cls, *values: object) -> int:
        """SQLite positional input follows the declared constructor; validate once."""
        try:
            cls(*values)
        except Exception:
            return 0
        return 1


@dataclass(frozen=True, slots=True)
class PublicationReceipt(TypedRow):
    @property
    def reference(self) -> MessageReference:
        return MessageReference(self.seq, self.message_id)

    execution_id: str = dataclass_field(metadata={"snapshot_exclude": True})
    publication_key: str = dataclass_field(metadata={"snapshot_exclude": True})
    seq: int
    message_id: str
    sender: str = dataclass_field(metadata={"snapshot_exclude": True})
    exact_target: str = dataclass_field(metadata={"snapshot_exclude": True})
    message_type: MessageType = dataclass_field(metadata={"snapshot_exclude": True})
    notice: bool = dataclass_field(metadata={"snapshot_exclude": True})
    timestamp: float = dataclass_field(metadata={"snapshot_exclude": True})
    payload_digest: str = dataclass_field(metadata={"snapshot_exclude": True})

    def __post_init__(self) -> None:
        for field, value in (
            ("execution_id", self.execution_id),
            ("publication_key", self.publication_key),
            ("message_id", self.message_id),
            ("sender", self.sender),
            ("exact_target", self.exact_target),
            ("payload_digest", self.payload_digest),
        ):
            require_nonempty(value, field)
        validate_execution_id(self.execution_id)
        for field, value in (
            ("sender", self.sender),
            ("message_id", self.message_id),
            ("payload_digest", self.payload_digest),
        ):
            require_bounded(value, field, MAX_IDENTIFIER_CHARS)
        object.__setattr__(self, "message_type", MessageType(self.message_type))
        if self.publication_key != canonical_publication_key(self.execution_id, self.exact_target):
            raise IntegrityViolationError("receipt publication key is not canonical")
        timestamp = float(self.timestamp)
        object.__setattr__(self, "timestamp", 0.0 if timestamp == 0.0 else timestamp)
        if not math.isfinite(self.timestamp) or self.timestamp < 0:
            raise ValueError("receipt timestamp must be finite and non-negative")
        object.__setattr__(self, "notice", bool(self.notice))
        if self.seq <= 0:
            raise ValueError("receipt seq must be positive")


@dataclass(frozen=True, kw_only=True)
class PublicationReceipts(CoordinatorTable, TypedTable):
    execution_id: str = dataclass_field(
        metadata={
            "sql": Column(
                primary_key=True,
                storage=ExactStorage,
                check=(
                    "typeof(execution_id) = 'text' AND length(execution_id) BETWEEN 1"
                    " AND 256\n"
                    "             AND instr(execution_id, ':') = 0"
                ),
            )
        }
    )
    exact_target: str = dataclass_field(metadata={"sql": Column(primary_key=True, storage=ExactStorage, check="length(exact_target) BETWEEN 1 AND 256")})
    seq: int = dataclass_field(metadata={"sql": Column(unique=True, check="seq > 0")})
    message_id: str = dataclass_field(
        metadata={
            "sql": Column(
                storage=ExactStorage,
                unique=True,
                check=(
                    "\n      typeof(message_id) = 'text' AND length(message_id) BETWEEN 1 AND 256"
                ),
            )
        }
    )
    received_at_ms: int = dataclass_field(metadata={"sql": Column(check="received_at_ms >= 0")})
    obligation_receipt_required: int | None = dataclass_field(
        init=False, compare=False, metadata={"sql": Column(generated="1")}
    )

    @classmethod
    def references(cls):
        from agent_comms.coordination_tables.responses import ResponseObligation

        return (
            ForeignKey(("execution_id", "exact_target"), PublicationIntents, ("execution_id", "exact_target")),
            ForeignKey(
                ("execution_id", "exact_target", "obligation_receipt_required"),
                ResponseObligation,
                ("execution_id", "exact_target", "receipt_settled"),
                deferred=True,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "publication_receipt_matches_authorities": (
                """CREATE TRIGGER publication_receipt_matches_authorities
BEFORE INSERT ON publication_receipts
BEGIN
    SELECT RAISE(ABORT, 'failed execution cannot accept publication receipt')
    WHERE EXISTS (SELECT 1 FROM executions
                  WHERE execution_id = NEW.execution_id AND status = 'failed');
    SELECT RAISE(ABORT, 'publication receipt message mismatch')
    WHERE NEW.message_id != (
        SELECT expected_message_id FROM publication_intents
        WHERE execution_id = NEW.execution_id AND exact_target = NEW.exact_target
    );
    SELECT RAISE(ABORT, 'publication receipt requires publishing obligation')
    WHERE NOT EXISTS (
        SELECT 1 FROM obligations
        WHERE execution_id = NEW.execution_id AND exact_target = NEW.exact_target AND state = 'publishing'
    );
END"""
            ),
            "publication_receipt_is_frozen": (
                """CREATE TRIGGER publication_receipt_is_frozen
BEFORE UPDATE ON publication_receipts
BEGIN
    SELECT RAISE(ABORT, 'publication receipt is frozen');
END"""
            ),
            "publication_receipt_delete_frozen": (
                """CREATE TRIGGER publication_receipt_delete_frozen
BEFORE DELETE ON publication_receipts
BEGIN
    SELECT RAISE(ABORT, 'publication receipt is frozen');
END"""
            ),
        }
