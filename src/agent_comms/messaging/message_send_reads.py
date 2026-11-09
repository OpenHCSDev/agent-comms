"""Presence replies use the original sender and committed reference, not a second send."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from agent_comms.execution.turn_item import NoSourceTurn, SourceTurn
from agent_comms.messaging.message_reference import MessageReference


class MessageReferenceRead(ABC):
    __slots__ = ()

    @property
    @abstractmethod
    def reference(self) -> MessageReference: ...


class PublishedMessageRead(MessageReferenceRead):
    __slots__ = ()
    at_ms: int


class CommittedMessageRead(ABC):
    __slots__ = ()
    message: MessageReferenceRead


class PresenceMessageSender(ABC):
    __slots__ = ()

    @abstractmethod
    def send(
        self, sender: str, target: str, body: str, *,
        answers: Sequence[MessageReference] = (), source_turn: SourceTurn = NoSourceTurn(),
    ) -> CommittedMessageRead: ...
