"""Original message-read capabilities, independent of live bus/table initialization."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import MessageId, ThreadLookup


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
