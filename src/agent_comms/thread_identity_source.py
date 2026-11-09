"""Identity reads shared by the original recorded and live thread declarations."""

from abc import ABC, abstractmethod

from agent_comms.registry.thread_identity import ThreadIncarnation


class ThreadIdentitySource(ABC):
    __slots__ = ()

    name: str

    @property
    @abstractmethod
    def incarnation(self) -> ThreadIncarnation: ...
