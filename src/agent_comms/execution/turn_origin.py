from __future__ import annotations

from abc import (
    ABC,
    abstractmethod,
)
from typing import TYPE_CHECKING

from agent_comms.input_key import InputKey

if TYPE_CHECKING:
    from agent_comms.input_disposition import InputDispositions
    from agent_comms.messages import Message
    from agent_comms.registry.threads import Thread


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
    def bus_keys(self, dispositions: InputDispositions, owner: Thread) -> tuple[InputKey, ...]: ...
