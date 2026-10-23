"""The common contract of message-driven and autonomous turn origins."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .input_disposition import InputDispositions
    from .messages import Message
    from .threads import Thread


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
    def bus_keys(self, dispositions: InputDispositions, owner: Thread) -> tuple[str, ...]: ...

