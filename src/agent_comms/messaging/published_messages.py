"""Original publication evidence consumed by transcript events."""

from abc import ABC, abstractmethod
from collections.abc import Iterable

from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import Message


class ReadsPublishedMessages(ABC):
    @abstractmethod
    def sent_messages(self, references: Iterable[MessageReference]) -> dict[MessageReference, Message]: ...
