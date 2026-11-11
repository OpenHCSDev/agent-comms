"""Actual goal and directive-store capabilities used by authored messages."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Container

from agent_comms.planning.owner_requests import OwnerRequestId, RequestDirective


class DirectiveWriter(ABC):
    __slots__ = ()

    @abstractmethod
    def record_directive(self, request: OwnerRequestId, directive: RequestDirective) -> None: ...


class MessageDirections[Message](ABC):
    __slots__ = ()
    registry: Container[str]
    todos: DirectiveWriter

    @abstractmethod
    def direct(self, name: str, message: Message) -> None: ...
