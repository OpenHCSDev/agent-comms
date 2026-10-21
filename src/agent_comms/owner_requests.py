"""Owner request identities, lifecycle, and original directive references."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, ClassVar, Self
from uuid import uuid4

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import BoundedText, SingleFieldValue

if TYPE_CHECKING:
    from agent_comms.goal_management import Goals
    from agent_comms.messages import Message
    from agent_comms.thread_identity import ThreadIncarnation
    from agent_comms.todos import Todo


@dataclass(frozen=True, slots=True)
class OwnerRequestId(SingleFieldValue):
    value: Annotated[str, BoundedText]

    @classmethod
    def new(cls) -> Self:
        return cls(uuid4().hex)


class RequestState(DeclaredFamily, affix="RequestState"):
    def require_open(self, request: OwnerRequestId) -> OpenRequestState:
        raise ValueError(f"Request {request.value} is already completed.")


@dataclass(frozen=True)
class OpenRequestState(RequestState):
    def require_open(self, request: OwnerRequestId) -> Self:
        return self

    def complete(
        self, request: OwnerRequestId, orders: Sequence[Todo], reason: str,
    ) -> CompletedRequestState:
        if any(order.pending for order in orders):
            raise ValueError(f"Request {request.value} still has pending orders.")
        return CompletedRequestState(reason)


@dataclass(frozen=True)
class CompletedRequestState(RequestState):
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("Completing an owner request requires a reason.")


@dataclass(frozen=True)
class RequestDirective:
    """One original directive; only a reply linked to that message answers it."""

    window: ClassVar[int] = 10 * 60
    seq: int
    deadline: float

    @property
    def due_time(self) -> str:
        return time.strftime("%H:%M", time.localtime(self.deadline))

    def original(self, goals: Goals) -> Message:
        with goals.bus.log.checked_read() as source:
            return source.delivery(self.seq).message

    def overdue(self, goals: Goals, owner: ThreadIncarnation, now: float) -> bool:
        if self.deadline >= now:
            return False
        original = self.original(goals).reference
        with goals.bus.log.checked_read() as source:
            replies = source.replies((original,))
        return not any(reply.message.sender == owner.name and original in reply.message.answers
                       for reply in replies)
