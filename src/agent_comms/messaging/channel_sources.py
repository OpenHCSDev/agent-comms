"""Original channel membership and history reads, without live thread assembly."""

from abc import ABC, abstractmethod

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
