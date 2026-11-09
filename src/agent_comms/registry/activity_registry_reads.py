"""Activity borrows current registry reads and the existing lease operations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping

from agent_comms.messaging.delivery_recipients import DeliveryRecipients
from agent_comms.messaging.frozen_recipient import FrozenRecipient
from agent_comms.registry.recipient_selection import RecipientSelection
from agent_comms.registry.thread_identity import OwnerIdentity
from agent_comms.registry.thread_status import ThreadStatus
from agent_comms.registry.threads import Thread


class ActivityRegistryRead(ABC):
    __slots__ = ()
    threads: Mapping[str, Thread]

    @abstractmethod
    def canonical_name(self, name: str) -> str: ...

    @abstractmethod
    def require(self, name: str) -> Thread: ...

    @abstractmethod
    def owner_identity(self, name: str) -> OwnerIdentity: ...

    @abstractmethod
    def status(self, name: str) -> ThreadStatus: ...

    @abstractmethod
    def claimed_readers(self, names: Iterable[str]) -> tuple[str, ...]: ...

    @abstractmethod
    def recipient(self, recipient: FrozenRecipient, original: DeliveryRecipients) -> RecipientSelection: ...
