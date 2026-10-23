"""Declared delivery and publication capabilities for SQLite bus records."""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.audience_manifest import FrozenAudience
from agent_comms.bus_tables import BusRow
from agent_comms.errors import (
    HumanSendRefusedError,
    CommittedMessageError,
    RelationViolationError,
)
from agent_comms.messages import BusRecord, Message
from agent_comms.response_acknowledgement import (
    KeyedResponseAcknowledgement,
)

from .field_codec import FieldCodec
from .wake_policy import RecipientDecision
from agent_comms.typed_table import Column, Generated, sql_literal

if TYPE_CHECKING:
    from .goal_management import Goals
    from agent_comms.bus import Bus, BusReader, RecordBuilder
    from .registry_document import RegistrySnapshot
    from .threads import Thread


class Delivered(BusRecord, declared_name=None):
    audience: FrozenAudience
    decisions: tuple[RecipientDecision, ...]
    control: str

    @property
    def sender_lookup(self) -> str:
        return self.audience.sender_lookup

    def projections(self):
        from .bus_tables import DirectReplySource, ReceivedSource, SentSource

        yield from super().projections()
        yield SentSource(self.audience.sender_lookup, self.seq)
        for recipient in self.audience.recipients:
            yield ReceivedSource(recipient.recipient_lookup, self.seq)
            if self.direct_for(recipient.recipient_lookup):
                yield DirectReplySource(recipient.recipient_lookup, self.seq)

    def deliveries(self) -> tuple[Self, ...]:
        return (self,)

    def direct_for(self, recipient_lookup: str) -> bool:
        """Direct reply semantics and the committed addressed incarnation agree."""
        policy = self.message.role.response_policy(self.message)
        return (policy.starts_turn and policy.separate_turn
                and self.audience.includes_lookup(recipient_lookup))

    def compaction_messages_for(self, recipient_lookup: str) -> tuple[Message, ...]:
        addressed = self.audience.includes_lookup(recipient_lookup)
        authored = (self.audience.sender_lookup == recipient_lookup
                    and self.message.retains_authored_task)
        return self.messages() if addressed or authored else ()


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


class InitialOrigin(ABC):
    """Who sends an initial message, checked under the bus lock before its append."""

    __slots__ = ()

    @abstractmethod
    def require_sender(
        self, message: Message, sender_thread: Thread, snapshot: RegistrySnapshot
    ) -> None:
        """Refuse a sender this origin cannot speak for."""

    @abstractmethod
    def require_source(self, source: BusReader, sender: str, message_id: str) -> None:
        """Check the sender and message ID on the actual append connection."""

    @abstractmethod
    def append(self, bus: Bus, build: RecordBuilder) -> BusRecord:
        """Commit the record, reporting failure as this origin requires."""

    @abstractmethod
    def published(self, goals: Goals, message: Message) -> None:
        """What a committed message from this origin records beyond the bus."""


class OwnerVoiceOrigin(InitialOrigin):
    """An origin that speaks for the owner; its message may direct the thread it reaches."""

    __slots__ = ()

    def published(self, goals: Goals, message: Message) -> None:
        message.task.direct(goals, message)


@dataclass(frozen=True, slots=True)
class AgentOrigin(InitialOrigin):
    """An executable thread's owner; it may resend under the same message id."""

    def published(self, goals: Goals, message: Message) -> None:
        """An agent's message directs no thread: only the owner's voice does."""

    def require_sender(
        self, message: Message, sender_thread: Thread, snapshot: RegistrySnapshot
    ) -> None:
        sender_thread.role.require_executable(
            RelationViolationError("Initial sender must be a visible registered executable.")
        )

    def require_source(self, source: BusReader, sender: str, message_id: str) -> None:
        """An agent's resend is idempotent, so the source holds no precondition."""

    def append(self, bus: Bus, build: RecordBuilder) -> BusRecord:
        return bus.append(build)


@dataclass(frozen=True, slots=True)
class DirectorOrigin(OwnerVoiceOrigin, AgentOrigin):
    """claude-ai's thread, which speaks for the owner on the bus."""


@dataclass(frozen=True, slots=True)
class HumanOrigin(OwnerVoiceOrigin):
    """Cooperative local UI origin, checked against the registered USER under lock.

    This is not a cryptographic credential against another process of this UID.
    It never grants an executable owner, claim, tool, or selected source authority.
    """

    sender: str
    created_at: float
    worktree: str

    @classmethod
    def of(cls, user: Thread) -> HumanOrigin:
        return cls(user.name, user.created_at, user.worktree)

    def require_sender(
        self, message: Message, sender_thread: Thread, snapshot: RegistrySnapshot
    ) -> None:
        self.require_registered(snapshot)
        if message.sender != self.sender:
            raise RelationViolationError("Local USER origin differs from registered identity.")

    def require_source(self, source: BusReader, sender: str, message_id: str) -> None:
        from .bus_tables import MessageIdentitySource

        with source.observing() as db:
            if MessageIdentitySource.exists(db, sender=sender, message_id=message_id):
                raise HumanSendRefusedError(RelationViolationError(
                    "Human message identity was already published."))

    def append(self, bus: Bus, build: RecordBuilder) -> BusRecord:
        try:
            return bus.append(build)
        except (CommittedMessageError, HumanSendRefusedError):
            raise
        except (OSError, sqlite3.Error, ValueError) as error:
            # The transaction owns rollback for storage and declared-data refusals.
            # Programming errors propagate as defects, not ordinary send refusals.
            raise HumanSendRefusedError(error) from error

    def require_registered(self, snapshot: RegistrySnapshot) -> None:
        """Check the determining USER declaration, never a transport or prose role."""
        user = snapshot.threads.get(self.sender)
        if user is None:
            raise RelationViolationError("Local USER origin is no longer registered")
        user.role.require_user(
            RelationViolationError("A user correction requires the original human sender")
        )
        registered = (user.name, user.created_at, user.worktree)
        if registered != (self.sender, self.created_at, self.worktree):
            raise RelationViolationError("Local USER origin differs from registered identity")
