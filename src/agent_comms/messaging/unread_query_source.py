"""The unread queries consumed by a bus count, without acquiring a registry."""

from abc import ABC, abstractmethod

from agent_comms.messaging.read_basis import UnreadQuery


class UnreadQuerySource(ABC):
    __slots__ = ()

    @abstractmethod
    def queries(self) -> tuple[UnreadQuery, ...]: ...
