"""Activity borrows current registry reads and the existing lease operations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from pathlib import Path

from agent_comms.execution.turn_lease import TurnCompletion, TurnLeaseFence
from agent_comms.messaging.delivery_recipients import DeliveryRecipients
from agent_comms.messaging.frozen_recipient import FrozenRecipient
from agent_comms.messaging.routing import NoTurnRouting, Routing
from agent_comms.nominal.field_codec import TurnId
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.registry.recipient_selection import RecipientSelection
from agent_comms.registry.thread_identity import OwnerIdentity
from agent_comms.registry.thread_status import ThreadStatus
from agent_comms.registry.threads import NativeThread, Thread


class RegistryPathRead(ABC):
    __slots__ = ()
    path: Path


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


class ActivityRegistration(ABC):
    __slots__ = ()
    store: RegistryPathRead
    storage_rule: StorageRule

    @abstractmethod
    def snapshot(self) -> ActivityRegistryRead: ...

    @abstractmethod
    def require(self, name: str) -> Thread: ...

    @abstractmethod
    def lease_local_turn(
        self, name: str, turn_id: TurnId, *, routing: Routing = NoTurnRouting(),
    ) -> tuple[NativeThread, int]: ...

    @abstractmethod
    def release_turn(self, lease: TurnLeaseFence) -> tuple[bool, TurnCompletion]: ...
