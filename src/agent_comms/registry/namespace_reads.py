"""Recorded and live namespace callbacks, supplied by the original registry values."""
from __future__ import annotations
from abc import ABC, abstractmethod
from agent_comms.messaging.frozen_recipient import FrozenRecipient
from agent_comms.registry.registry_names import RegistryNames
from agent_comms.registry.thread_identity_source import ThreadIdentitySource


class NamespaceRead[Member: ThreadIdentitySource, Selection](RegistryNames[Member], ABC):
    __slots__ = ()

    @abstractmethod
    def displayed_recipient_effect(self, reads, message, thread: Member, *, document=None): ...

    @abstractmethod
    def historical_recipient(self, recipient: FrozenRecipient) -> Selection: ...
