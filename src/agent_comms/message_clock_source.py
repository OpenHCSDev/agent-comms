"""Original recorded message clocks, below destination and persistence initialization."""

from abc import ABC, abstractmethod

from agent_comms.storage.recorded_time import Timestamped


class SenderClockSource(ABC):
    __slots__ = ()

    @classmethod
    @abstractmethod
    def last_user_input(cls, previous: float, timestamp: float) -> float: ...


class MessageClockSource(Timestamped):
    __slots__ = ()

    sender_role: type[SenderClockSource]
