"""Owner request identities, lifecycle, and original directive references."""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, ClassVar, Self
from uuid import uuid4

from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import (
    BoundedText,
    NullMember,
    RecordMember,
    SingleFieldValue,
    SubclassUnion,
)

if TYPE_CHECKING:
    from agent_comms.channel_targets import NamedTarget
    from agent_comms.messages import Message
    from agent_comms.registration import Registration
    from agent_comms.thread_identity import ThreadIncarnation
    from agent_comms.todos import OwnerRequest, Todo, TodoStore
    from agent_comms.bus import BusReader


class RequestValue(SubclassUnion, affix=""):
    """One id can be a stored link and an explicitly selected request."""


class RequestLink(RequestValue, declared_name=None):
    @abstractmethod
    def require(self) -> OwnerRequestId: ...


class RequestSelection(RequestValue, declared_name=None):
    def for_message(
        self,
        store: TodoStore,
        target: NamedTarget,
        registry: Registration,
    ) -> tuple[OwnerRequest, ...]:
        return tuple(
            request
            for name in target.direct_names
            for request in self.select(store, registry.require(name).incarnation)
        )

    def for_order(self, store: TodoStore, commander: ThreadIncarnation) -> OwnerRequestId:
        selected = self.select(store, commander)
        if len(selected) != 1:
            raise RelationViolationError("An order needs exactly one open owner request.")
        return selected[0].id

    @abstractmethod
    def select(
        self,
        store: TodoStore,
        commander: ThreadIncarnation,
    ) -> tuple[OwnerRequest, ...]: ...


@dataclass(frozen=True, slots=True)
class OwnerRequestId(SingleFieldValue, RequestLink, RequestSelection):
    value: Annotated[str, BoundedText]

    @classmethod
    def new(cls) -> Self:
        return cls(uuid4().hex)

    def require(self) -> Self:
        return self

    def for_message(
        self,
        store: TodoStore,
        target: NamedTarget,
        registry: Registration,
    ) -> tuple[OwnerRequest, ...]:
        return self.select(store, registry.require(target.require_dm_peer()).incarnation)

    def select(
        self,
        store: TodoStore,
        commander: ThreadIncarnation,
    ) -> tuple[OwnerRequest, ...]:
        request = store.owner_request(self)
        request.require_open(commander)
        return (request,)


@dataclass(frozen=True)
class NoRequestLink(NullMember, RequestLink):
    def require(self) -> OwnerRequestId:
        raise RelationViolationError("An unordered problem has no owner request.")


@dataclass(frozen=True)
class NewestOpenRequest(NullMember, RequestSelection):
    def select(
        self,
        store: TodoStore,
        commander: ThreadIncarnation,
    ) -> tuple[OwnerRequest, ...]:
        return store.open_requests(commander, limit=1)


class RequestState(DeclaredFamily, affix="RequestState"):
    def require_open(self, request: OwnerRequestId) -> OpenRequestState:
        raise ValueError(f"Request {request.value} is already completed.")


@dataclass(frozen=True)
class OpenRequestState(RequestState):
    def require_open(self, request: OwnerRequestId) -> Self:
        return self

    def complete(
        self,
        request: OwnerRequestId,
        orders: Sequence[Todo],
        reason: str,
    ) -> CompletedRequestState:
        if orders:
            raise ValueError(f"Request {request.value} still has pending orders.")
        return CompletedRequestState(reason)


@dataclass(frozen=True)
class CompletedRequestState(RequestState):
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("Completing an owner request requires a reason.")


class RequestRevision(SubclassUnion, affix="RequestRevision"):
    @abstractmethod
    def validate(self, requests: Mapping[OwnerRequestId, OwnerRequest]) -> None: ...


@dataclass(frozen=True)
class NoRequestRevision(NullMember, RequestRevision):
    def validate(self, requests: Mapping[OwnerRequestId, OwnerRequest]) -> None:
        pass


@dataclass(frozen=True)
class RequestAtRevision(RecordMember, RequestRevision):
    """A historical directive prefix, not another writable request state."""

    id: OwnerRequestId
    directive_count: int

    def __post_init__(self) -> None:
        if self.directive_count < 0:
            raise ValueError("A directive prefix cannot have negative length.")

    def validate(self, requests: Mapping[OwnerRequestId, OwnerRequest]) -> None:
        request = requests[self.id]
        if self.directive_count > len(request.directives):
            raise RelationViolationError("Request history exceeds its recorded directives.")


@dataclass(frozen=True)
class RequestDirective:
    """One original directive; only a reply linked to that message answers it."""

    window: ClassVar[int] = 10 * 60
    seq: int
    deadline: float

    @property
    def due_time(self) -> str:
        return time.strftime("%H:%M", time.localtime(self.deadline))

    @classmethod
    def from_message(cls, message: Message) -> Self:
        return cls(message.seq, message.timestamp + cls.window)

    def original(self, source: BusReader) -> Message:
        return source.lookup(self.seq)
