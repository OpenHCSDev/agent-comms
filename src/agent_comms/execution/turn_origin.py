from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from agent_comms.input_key import InputKey
from agent_comms.registry.thread_creation import ThreadCreationSource

if TYPE_CHECKING:
    from agent_comms.messaging.messages import Message


class TurnOrigin(ABC):
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
    def messages(self) -> tuple[Message, ...]: ...

    @abstractmethod
    def bus_keys(self, owner: ThreadCreationSource) -> tuple[InputKey, ...]: ...
