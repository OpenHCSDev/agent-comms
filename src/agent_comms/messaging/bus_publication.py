"""Declared delivery and publication capabilities for SQLite bus records."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import ClassVar, Self

from agent_comms.coordination.response_acknowledgement import KeyedResponseAcknowledgement
from agent_comms.errors import RelationViolationError
from agent_comms.messaging.audience_manifest import AudiencedMessage
from agent_comms.messaging.messages import RecordBuilder
from agent_comms.messaging.bus_tables import AwaitingAnswerSource, ReceivedSource, SentSource
from agent_comms.messaging.channel_targets import ThreadTarget
from agent_comms.messaging.delivery_recipients import DeliveryRecipients
from agent_comms.messaging.initial_origin import InitialOrigin, OwnerVoiceOrigin
from agent_comms.messaging.message_directions import MessageDirections
from agent_comms.messaging.message_reads import MessageDeliveryRead, MessageIdentityRead
from agent_comms.messaging.message_send_reads import PublishedMessageRead
from agent_comms.messaging.messages import MessageWriter
from agent_comms.messaging.messages import BusRecord, BusRow, Message
from agent_comms.messaging.projection_source import ProducedRow
from agent_comms.nominal.class_fact import ClassFact
from agent_comms.nominal.field_codec import FieldCodec, MessageId, ThreadLookup
from agent_comms.registry.task_owner import TaskOwner
from agent_comms.registry.task_registry import TaskRegistry
from agent_comms.storage.typed_table import Column, Generated, sql_literal
from agent_comms.waking.wake_policy import RecipientDecision


class Delivered(
    BusRecord,
    AudiencedMessage,
    MessageDeliveryRead[Message],
    DeliveryRecipients,
    PublishedMessageRead,
    declared_name=None,
):

    @ClassFact
    def writes(cls) -> tuple[type[ProducedRow], ...]:
        return (SentSource, ReceivedSource, AwaitingAnswerSource)

    decisions: tuple[RecipientDecision, ...]
    control: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if len(self.decisions) != len(self.recipients):
            raise ValueError("Frozen recipients and their decisions must have equal length")

    def projections(self):
        yield from super().projections()
        yield SentSource(self.sender_lookup, self.seq)
        for recipient in self.recipients:
            yield ReceivedSource(recipient.recipient_lookup, self.seq)
            if self.awaits_answer_from(ThreadTarget(recipient.canonical_thread)):
                yield AwaitingAnswerSource(
                    recipient.recipient_lookup, self.seq, recipient.canonical_thread,
                    self.role.response_policy(self).starts_direct_turn,
                )

    @property
    def history_record(self) -> Self:
        """A copied delivery keeps its frozen audience: its recipients received it there too."""
        return self

    def deliveries(self) -> tuple[Self, ...]:
        return (self,)

    def direct_for(self, recipient_lookup: ThreadLookup) -> bool:
        """Direct reply semantics and the committed addressed incarnation agree."""
        policy = self.message.role.response_policy(self.message)
        return (policy.starts_direct_turn
                and self.includes_lookup(recipient_lookup))


class KeyedPublication(BusRecord, declared_name=None):
    """One publication key binds Message content, not the acknowledgement encoding."""

    acknowledgement: KeyedResponseAcknowledgement
    publication_key: ClassVar[Generated] = Generated(
        str, Column(generated=(
            "json_extract(acknowledgement, "
            + sql_literal("$." + json.dumps(
                FieldCodec.field_plan(KeyedResponseAcknowledgement, "publication_key").key,
                ensure_ascii=False,
            )) + ")"
        ), unique=True),
    )

    @classmethod
    def publication_sequences(cls, db: sqlite3.Connection, publication_key: str) -> tuple[int, ...]:
        return tuple(row.seq for row in KeyedPublication.table.select(
            db, publication_key=publication_key, limit=1,
        ))

    def append_to(self, db: sqlite3.Connection, seq: int) -> int:
        for original_seq in self.publication_sequences(db, self.acknowledgement.publication_key):
            header = BusRow.require(db, seq=original_seq)
            original = header.kind.read_from(db, original_seq)
            if self.publication_snapshot != original.publication_snapshot:
                raise RelationViolationError(
                    "Response publication key has different message content."
                )
            return original_seq
        return super().append_to(db, seq)


@dataclass(frozen=True, slots=True)
class AutomatedOrigin(InitialOrigin):
    """Shared non-human publication, without an implied owner turn or directive."""

    def published(self, goals: MessageDirections[Message], message: Message) -> None:
        """An automated message directs no thread: only the owner's voice does."""

    def require_sender(
        self, message: Message, sender_thread: TaskOwner, snapshot: TaskRegistry[TaskOwner]
    ) -> None:
        sender_thread.role.require_automated_publication(
            RelationViolationError("Initial sender must be a registered automated publisher.")
        )

    def require_source(self, source: MessageIdentityRead, sender: str, message_id: MessageId) -> None:
        """A resend is decided by the bus's own write, not by a read before it."""

    def append(self, bus: MessageWriter, build: RecordBuilder) -> BusRecord:
        """A resend of the same message returns its committed original, never a copy."""
        return bus.append_original(build)


@dataclass(frozen=True, slots=True)
class AgentOrigin(AutomatedOrigin):
    """An executable thread's owner; it may resend under the same message id."""

    def require_sender(
        self, message: Message, sender_thread: TaskOwner, snapshot: TaskRegistry[TaskOwner]
    ) -> None:
        sender_thread.role.require_executable(
            RelationViolationError("Initial sender must be a visible registered executable.")
        )


@dataclass(frozen=True, slots=True)
class ServiceOrigin(AutomatedOrigin):
    """A service publisher does not impersonate an executable colleague."""

    def require_sender(
        self, message: Message, sender_thread: TaskOwner, snapshot: TaskRegistry[TaskOwner]
    ) -> None:
        sender_thread.role.require_service_publication(
            RelationViolationError("Initial sender must be a declared service publisher.")
        )


@dataclass(frozen=True, slots=True)
class DirectorOrigin(OwnerVoiceOrigin, AgentOrigin):
    """claude-ai's thread, which speaks for the owner on the bus."""
