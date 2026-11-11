from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass



from agent_comms.errors import CommittedMessageError, HumanSendRefusedError, RelationViolationError
from agent_comms.messaging.message_directions import MessageDirections
from agent_comms.messaging.message_reads import MessageIdentityRead
import agent_comms.messaging.messages as messages
from agent_comms.nominal.field_codec import MessageId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.task_owner import TaskOwner
from agent_comms.registry.task_registry import TaskRegistry


class InitialOrigin(ABC):
    """Who sends an initial message, checked under the bus lock before its append."""

    __slots__ = ()

    @abstractmethod
    def require_sender(
        self, message: messages.Message, sender_thread: TaskOwner, snapshot: TaskRegistry[TaskOwner]
    ) -> None:
        """Refuse a sender this origin cannot speak for."""

    @abstractmethod
    def require_source(self, source: MessageIdentityRead, sender: str, message_id: MessageId) -> None:
        """Check the sender and message ID on the actual append connection."""

    @abstractmethod
    def append(self, bus: messages.MessageWriter, build: messages.RecordBuilder) -> messages.BusRecord:
        """Commit the record, reporting failure as this origin requires."""

    @abstractmethod
    def published(self, goals: MessageDirections[messages.Message], message: messages.Message) -> None:
        """What a committed message from this origin records beyond the bus."""

    def complete(self, goals: MessageDirections[messages.Message], record: messages.BusRecord) -> messages.BusRecord:
        """Keep the actual committed record when an origin's remaining work fails."""
        try:
            self.published(goals, record.message)
        except BaseException as error:
            raise CommittedMessageError(record) from error
        return record


class OwnerVoiceOrigin(InitialOrigin):
    """An origin that speaks for the owner; its message may direct the thread it reaches."""

    __slots__ = ()

    def published(self, goals: MessageDirections[messages.Message], message: messages.Message) -> None:
        message.task.direct(goals, message)


@dataclass(frozen=True, slots=True)
class HumanOrigin(OwnerVoiceOrigin):
    """Cooperative local UI origin, checked against the registered USER under lock.

    This is not a cryptographic credential against another process of this UID.
    It never grants an executable owner, claim, tool, or selected source authority.
    """

    sender: str
    created_at: float
    worktree: Checkout

    @classmethod
    def of(cls, user: TaskOwner) -> HumanOrigin:
        return cls(user.name, user.incarnation.wire_birth, user.worktree)

    def require_sender(
        self, message: messages.Message, sender_thread: TaskOwner, snapshot: TaskRegistry[TaskOwner]
    ) -> None:
        self.require_registered(snapshot)
        if message.sender != self.sender:
            raise RelationViolationError("Local USER origin differs from registered identity.")

    def require_source(self, source: MessageIdentityRead, sender: str, message_id: MessageId) -> None:
        if source.contains_message_identity(sender, message_id):
            raise HumanSendRefusedError(RelationViolationError(
                "Human message identity was already published."))

    def append(self, bus: messages.MessageWriter, build: messages.RecordBuilder) -> messages.BusRecord:
        try:
            return bus.append(build)
        except (CommittedMessageError, HumanSendRefusedError):
            raise
        except (OSError, sqlite3.Error, ValueError) as error:
            # The transaction owns rollback for storage and declared-data refusals.
            # Programming errors propagate as defects, not ordinary send refusals.
            raise HumanSendRefusedError(error) from error

    def require_registered(self, snapshot: TaskRegistry[TaskOwner]) -> None:
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
