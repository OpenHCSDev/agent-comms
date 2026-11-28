"""Original channel membership and history reads, without live thread assembly."""

from __future__ import annotations

from abc import ABC, abstractmethod

from agent_comms.registry.registry_names import RegistryNames
from agent_comms.registry.tagged_thread_source import TaggedThreadSource
from agent_comms.registry.thread_role_source import ThreadRoleSource


class ChannelMemberSource(TaggedThreadSource, ThreadRoleSource):
    __slots__ = ()


class ChannelHistorySource(ABC):
    __slots__ = ()

    name: str
    tags: frozenset[str]

    @property
    @abstractmethod
    def history_targets(self) -> frozenset[str]: ...


class ChannelScopeFactory[Scope](ABC):
    """Constructs a channel's display scope without coupling channel declarations to read_basis."""

    __slots__ = ()

    @classmethod
    @abstractmethod
    def channel_scope(cls, channel: str, targets: frozenset[str]) -> Scope: ...

    @classmethod
    @abstractmethod
    def all_channel_scope(cls, channel: str) -> Scope: ...

    @classmethod
    @abstractmethod
    def member_activity_scope(
        cls, channel: ChannelHistorySource, snapshot: RegistryNames[TaggedThreadSource]
    ) -> Scope: ...
