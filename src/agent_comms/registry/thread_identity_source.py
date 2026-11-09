"""Identity reads shared by the original recorded and live thread declarations."""

from abc import abstractmethod

from agent_comms.registry.thread_creation import ThreadCreationSource
from agent_comms.registry.thread_identity import ThreadIncarnation


class ThreadIdentitySource(ThreadCreationSource):
    __slots__ = ()

    name: str

    @property
    @abstractmethod
    def incarnation(self) -> ThreadIncarnation: ...
