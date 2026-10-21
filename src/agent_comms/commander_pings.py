"""Typed commander decisions and references to their committed deliveries."""
from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self, cast

from agent_comms.bus_publication import AgentOrigin
from agent_comms.declared_family import DeclaredFamily
from agent_comms.message_page import MessageReadRequest
from agent_comms.message_reference import MessageReference
from agent_comms.messages import CommanderPingKind, Message
from agent_comms.read_basis import DMDisplayScope
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.todos import CommanderCursor, ListedPullRequest, PullRequestLink, Todo
from agent_comms.ui_model.unit_state import UnitState

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True)
class OrderKey:
    problem: str
    order: str


@dataclass(frozen=True)
class OrderRevision(OrderKey):
    revision: int

    @classmethod
    def of(cls, todo: Todo) -> Self:
        return cls(todo.id, todo.order.checked().id, todo.revision)

    @property
    def key(self) -> OrderKey:
        return OrderKey(self.problem, self.order)


@dataclass(frozen=True)
class PingTopic:
    unit: ThreadIncarnation
    kind: type[Ping]
    orders: tuple[OrderKey, ...]


@dataclass(frozen=True)
class PullRequestTopic(PingTopic):
    link: PullRequestLink


@dataclass(frozen=True)
class Ping(DeclaredFamily, affix="Ping"):
    unit: ThreadIncarnation
    state: type[UnitState]
    orders: tuple[OrderRevision, ...]

    @property
    def topic(self) -> PingTopic:
        return PingTopic(self.unit, type(self), tuple(order.key for order in self.orders))

    @property
    @abstractmethod
    def event(self) -> object: ...

    action: ClassVar[str]

    @property
    def default_action(self) -> str:
        return self.action

    @abstractmethod
    def line(self) -> str: ...

    def previous(self, known: Iterable[DeliveredPing]) -> Self | None:
        return next((cast(Self, item.ping) for item in known if item.ping.topic == self.topic), None)

    def new_since(self, known: Iterable[DeliveredPing]) -> bool:
        previous = self.previous(known)
        return previous is None or previous.event != self.event


@dataclass(frozen=True)
class UnitStatePing(Ping):
    detail: str
    action: str

    @property
    def topic(self) -> PingTopic:
        return PingTopic(self.unit, type(self), ())

    @property
    def event(self) -> object:
        return self.state

    def new_since(self, known: Iterable[DeliveredPing]) -> bool:
        previous = self.previous(known)
        return (previous is None
                or tuple(order.key for order in self.orders) != tuple(order.key for order in previous.orders)
                or self.state is not previous.state and issubclass(self.state, previous.state))

    def line(self) -> str:
        return (f"{self.state.declared_name.replace('_', ' ').upper()} {self.unit.name}: "
                f"{self.detail} -> {self.default_action}")


@dataclass(frozen=True)
class ReviewTimerPing(Ping):
    action = "read its diff and judge the head"
    due: float

    @property
    def event(self) -> object:
        return self.due

    def line(self) -> str:
        return (f"TIMER UP {self.unit.name} {self.orders[0].problem} "
                f"at {RecordedEventTime(self.due).clock()} -> {self.default_action}")


@dataclass(frozen=True)
class PullRequestChange:
    head: str
    status: type[ListedPullRequest]


@dataclass(frozen=True)
class PullRequestPing(Ping):
    link: PullRequestLink
    change: PullRequestChange

    @property
    def topic(self) -> PingTopic:
        return PullRequestTopic(self.unit, type(self), tuple(order.key for order in self.orders), self.link)

    @property
    def event(self) -> object:
        return self.change

    @property
    def default_action(self) -> str:
        return self.change.status.action

    def line(self) -> str:
        return (f"PR #{self.link.number} {self.unit.name} {self.orders[0].problem}: "
                f"{self.change.status.declared_name.replace('_', ' ')} -> {self.default_action}")


@dataclass(frozen=True)
class FailedCheckPing(Ping):
    action = "read the failed delivery check and order its repair"
    failed_at: float

    @property
    def event(self) -> object:
        return self.failed_at

    def line(self) -> str:
        return f"CHECK FAILED {self.unit.name} {self.orders[0].problem} -> {self.default_action}"


@dataclass(frozen=True)
class MergePing(Ping):
    action = "install the merge and run the affected application"
    commit: str

    @property
    def event(self) -> object:
        return self.commit

    def line(self) -> str:
        return (f"MERGED {self.unit.name} {self.orders[0].problem} "
                f"{self.commit[:9]} -> {self.default_action}")


@dataclass(frozen=True)
class InstallPing(Ping):
    action = "read the application evidence and close the work"
    artifact: str

    @property
    def event(self) -> object:
        return self.artifact

    def line(self) -> str:
        return f"INSTALLED {self.unit.name} {self.orders[0].problem} -> {self.default_action}"


@dataclass(frozen=True)
class PingReference:
    message: MessageReference
    index: int


@dataclass(frozen=True)
class DeliveredPing:
    """A read of original delivery evidence, never a retained copy of PR state."""
    reference: PingReference
    ping: Ping


@dataclass(frozen=True, slots=True, kw_only=True)
class CommanderPingMessage(Message):
    commander: ThreadIncarnation
    pings: tuple[Ping, ...]
    prior: tuple[PingReference, ...]

    @classmethod
    def send(cls, comms: Comms, sender: str, commander: ThreadIncarnation,
             pings: tuple[Ping, ...], known: tuple[DeliveredPing, ...]) -> CommanderPingMessage:
        replaced = {ping.topic for ping in pings}
        message = cls(sender=sender, target=commander.name, type=CommanderPingKind,
            body="\n".join(ping.line() for ping in pings), commander=commander, pings=pings,
            prior=tuple(item.reference for item in known if item.ping.topic not in replaced))
        # Publisher owns recipient, claim and publication validation. No raw bus append.
        committed = cast(Self, comms.bus.publisher.publish_ordinary(message, origin=AgentOrigin()))
        CommanderCursor(commander, committed.reference).advance(comms.goals.todos)
        return committed

    def delivered(self, comms: Comms) -> tuple[DeliveredPing, ...]:
        known = []
        with comms.bus.log.checked_read() as source:
            references = tuple(dict.fromkeys(item.message for item in self.prior))
            messages = {delivery.message.reference: cast(CommanderPingMessage, delivery.message)
                        for delivery in source.references(references)}
        for reference in self.prior:
            original = messages[reference.message]
            if original.commander != self.commander:
                raise ValueError("A ping reference changed commander incarnation")
            known.append(DeliveredPing(reference, original.pings[reference.index]))
        known.extend(DeliveredPing(PingReference(self.reference, index), ping)
                     for index, ping in enumerate(self.pings))
        return tuple(known)


@dataclass(frozen=True, slots=True)
class LatestCommanderPing(MessageReadRequest[CommanderPingMessage | None]):
    commander: ThreadIncarnation
    after: MessageReference | None = None
    empty: ClassVar[None] = None

    def indexed(self, index, source) -> CommanderPingMessage | None:
        with closing(index.recent_offsets(
            self.commander.created_at, predicate=self.scope.index_predicate,
            before_offset=source.boundary,
        )) as offsets:
            for row in self.indexed_records(source, offsets):
                message = row.message
                if self.after is not None and message.seq <= self.after.seq:
                    break
                if message.type is CommanderPingKind:
                    ping = cast(CommanderPingMessage, message)
                    if ping.commander == self.commander:
                        return ping
        return None

    @classmethod
    def read(cls, comms: Comms, sender: str,
             commander: ThreadIncarnation) -> CommanderPingMessage | None:
        with comms.goals.todos.snapshot() as db:
            position = CommanderCursor.one(db, commander=commander)
        after = position.message if position is not None else None
        latest = comms.bus.log.read_page(cls(
            DMDisplayScope.capture(sender, commander.name, comms.registry.snapshot()), commander, after))
        if latest is not None or position is None:
            return latest
        with comms.bus.log.checked_read() as source:
            original, = source.references((position.message,))
        message = cast(CommanderPingMessage, original.message)
        if message.commander != commander:
            raise ValueError("Commander cursor references another incarnation")
        return message
