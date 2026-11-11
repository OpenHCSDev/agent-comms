from abc import ABC, abstractmethod

from agent_comms.execution.input_key import InputKey
from agent_comms.registry.thread_creation import ThreadCreationSource


class TurnOrigin[MessageType](ABC):
    """The source of a scheduled turn owns batching and delivery identity."""

    __slots__ = ()

    @property
    @abstractmethod
    def separate_turn(self) -> bool: ...

    @property
    @abstractmethod
    def batch_target(self) -> object: ...

    @property
    @abstractmethod
    def autonomous(self) -> bool: ...

    @abstractmethod
    def messages(self) -> tuple[MessageType, ...]: ...

    @abstractmethod
    def bus_keys(self, owner: ThreadCreationSource) -> tuple[InputKey, ...]: ...
