"""Original message-read capabilities, independent of live bus/table initialization."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
import sqlite3

from agent_comms.messaging.channel_targets import ThreadTarget
from agent_comms.messaging.mentions import ThreadMention
from agent_comms.messaging.message_directions import MessageDirections
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import MessageId, ThreadLookup


class MessageReferenceRead(ABC):
    __slots__ = ()

    @property
    @abstractmethod
    def reference(self) -> MessageReference: ...


class ScopedMessage(MessageReferenceRead):
    """Addressed message fields checked by display scopes without importing bus tables."""
    __slots__ = ()
    sender: str
    target: str
    mentions: tuple[ThreadMention, ...]
    answers: tuple[MessageReference, ...]

    @property
    @abstractmethod
    def seq(self) -> int: ...

    @abstractmethod
    def awaits_answer_from(self, participant: ThreadTarget) -> bool: ...


class DirectableMessage[Message](ABC):
    """A message whose sender can be checked and whose task can direct owner goals."""
    __slots__ = ()
    sender: str

    @abstractmethod
    def direct(self, goals: MessageDirections[Message]) -> None: ...


class PublishedRecord[Message](DirectableMessage[Message]):
    """A committed bus record carrying its authored message."""
    __slots__ = ()

    @property
    @abstractmethod
    def message(self) -> Message: ...


type RecordBuilder[Record] = Callable[[int, sqlite3.Connection], Record]
"""The original bus append operations used by initial publication."""


class MessageWriter[Record](ABC):
    """The bus append interface used by initial publication origins."""
    __slots__ = ()

    @abstractmethod
    def append(self, build: RecordBuilder[Record]) -> Record: ...

    @abstractmethod
    def append_original(self, build: RecordBuilder[Record]) -> Record: ...


class ConversationRead[Record](ABC):
    __slots__ = ()

    @abstractmethod
    def conversation(
        self, lookup: ThreadLookup, *, after: int = 0, before: int | None = None,
        limit: int = 100, descending: bool = True, sequences: tuple[int, ...] = (),
    ) -> tuple[Record, ...]: ...


class MessageDeliveryRead[Message](ABC):
    __slots__ = ()
    sender_lookup: ThreadLookup

    @property
    @abstractmethod
    def message(self) -> Message: ...

    @abstractmethod
    def includes_lookup(self, lookup: ThreadLookup) -> bool: ...


class MessageReferencesRead[Message](ABC):
    __slots__ = ()

    @abstractmethod
    def messages_for_references(
        self, references: Iterable[MessageReference],
    ) -> tuple[Message, ...]: ...

    @abstractmethod
    def deliveries_for_references(
        self, references: Iterable[MessageReference],
    ) -> tuple[MessageDeliveryRead[Message], ...]: ...


class MessageIdentityRead(ABC):
    __slots__ = ()

    @abstractmethod
    def contains_message_identity(self, sender: str, message_id: MessageId) -> bool: ...
