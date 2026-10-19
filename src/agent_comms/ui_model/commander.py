"""Read-only commander cards, derived from Core's order and transcript owners.

The observer prepares these models. A frontend supplies only commander identity
and expansion interest; it never reads stores, runs checks or decides order state.
"""
from __future__ import annotations

import time
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.coordination_tables.refusals import SendRefusal
from agent_comms.declared_family import DeclaredFamily
from agent_comms.notification_progress import FailedProgress
from agent_comms.presentation import NoRevision, ReadState, WireRevision
from agent_comms.revision_views import CommanderView
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.todos import OrderProgress, ProgressSource, PullRequests, Todo
from agent_comms.turn_digests import LastTurns, ThreadTurns
from agent_comms.ui_model.observation import (
    InterestRequest,
    Observation,
    ObservationRequest,
    Observed,
    ReadRequest,
    StoredRead,
    UnreadableHistory,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agent_comms.comms import Comms
    from agent_comms.ui_model.observation import ObservationService, ObservationStep


@dataclass(frozen=True)
class CommanderOrder:
    todo: Todo
    progress: OrderProgress
    overdue: bool

    @property
    def line(self) -> str:
        return (f"{'OVERDUE ' if self.overdue else ''}{self.todo.id}: "
                f"{' '.join(self.todo.text.split())} | due {self.todo.deadline.clock()} | "
                f"{self.todo.order.line}; {self.progress.state}")


class CardDetail:
    @property
    def text(self) -> str:
        return "Expand to read this unit's orders and recent turns."

    @property
    def turn_lines(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class OpenCardDetail(CardDetail):
    orders: tuple[CommanderOrder, ...]
    turns: ThreadTurns
    latest_commits: tuple[str, ...]

    @property
    def text(self) -> str:
        lines = [order.todo.order.instructions(order.todo)
                 + "\nLatest work: " + order.progress.state for order in self.orders]
        lines.append("Latest checkout commits:\n" + "\n".join(self.latest_commits))
        lines.append("Recent turns (newest first):")
        return "\n\n".join(lines)

    @property
    def turn_lines(self) -> tuple[str, ...]:
        return tuple(turn.line for turn in self.turns.turns) or ("No recorded turns.",)


@dataclass(frozen=True)
class UnitCard:
    unit: ThreadIncarnation
    orders: tuple[CommanderOrder, ...]
    detail: CardDetail

    @property
    def line(self) -> str:
        return self.unit.name + " | " + " ; ".join(order.line for order in self.orders)

    @property
    def priority(self) -> tuple[bool, float, str]:
        return (not any(order.overdue for order in self.orders),
                min(order.todo.deadline.recorded_at("An order deadline") for order in self.orders),
                self.unit.name.casefold())


@dataclass(frozen=True)
class CommanderAlarm:
    line: str


class CommanderAlarmSource(DeclaredFamily, affix="AlarmSource"):
    """New retained alarm sources extend the model, never the page's rendering.

    Retained pre-send refusals and failed deliveries have different source owners.
    Each source supplies its own lines; the frontend has no kind switches.
    """

    @classmethod
    @abstractmethod
    def read(cls, comms: Comms, commander: ThreadIncarnation,
             orders: tuple[CommanderOrder, ...], now: float) -> tuple[CommanderAlarm, ...]: ...


class StalledOrderAlarmSource(CommanderAlarmSource):
    @classmethod
    def read(cls, comms, commander, orders, now):
        return tuple(CommanderAlarm(
            f"STALLED {order.todo.held().owner} {order.todo.id}: {order.progress.state}")
            for order in orders if order.todo.pending
            and order.progress.stalled(order.todo.order.at.recorded_at("An order"), now))


class FailedDeliveryAlarmSource(CommanderAlarmSource):
    @classmethod
    def read(cls, comms, commander, orders, now):
        presentation = comms.views.thread_presentations((commander.name,)).require(commander.name)
        return tuple(CommanderAlarm(
            f"RECORDED FAILED DELIVERY to {notice.recipient}: {notice.label}")
            for notice in presentation.sent_notifications
            if isinstance(notice.state, FailedProgress))


class RefusedSendAlarmSource(CommanderAlarmSource):
    @classmethod
    def read(cls, comms, commander, orders, now):
        return tuple(CommanderAlarm(
            f"REFUSED SEND #{refusal.id} {refusal.sender} -> {refusal.target}: {refusal.reason}")
            for refusal in SendRefusal.recent(comms.root, sender=commander.name))


@dataclass(frozen=True)
class CommanderModel:
    commander: ThreadIncarnation
    units: tuple[UnitCard, ...]
    alarms: tuple[CommanderAlarm, ...]

    @classmethod
    def read(cls, comms: Comms, request: ObserveCommander) -> CommanderModel:
        if Path(request.root).resolve() != comms.root.resolve():
            raise ValueError("The commander page belongs to a different Comms root; reopen it.")
        commander = comms.registry.require(request.commander.name).incarnation
        if commander != request.commander:
            raise ValueError("The commander incarnation changed; reopen the page.")
        todos = comms.goals.todos.held_for_commander(commander)
        pulls, storage, now = PullRequests(todos), comms.registry.storage_rule, time.time()
        grouped: dict[ThreadIncarnation, list[CommanderOrder]] = {}
        for todo in todos:
            unit = todo.held().owner_identity
            progress = ProgressSource.at(
                todo, storage.order_checkout(unit.name, todo.id), pulls).progress()
            grouped.setdefault(unit, []).append(CommanderOrder(todo, progress, todo.overdue(now)))
        cards = []
        for unit, orders in grouped.items():
            if comms.registry.require(unit.name).incarnation != unit:
                raise ValueError(
                    f"Order holder {unit.name} changed incarnation; refresh its assignment.")
            current = tuple(orders)
            detail: CardDetail = CardDetail()
            if unit in request.expanded:
                commits = []
                for order in current:
                    source = ProgressSource.at(order.todo, storage.order_checkout(
                        unit.name, order.todo.id), pulls)
                    try:
                        commit = source.latest_commit()
                    except ValueError as error:
                        commit = f"unavailable: {error}"
                    commits.append(f"{order.todo.id}: {commit}")
                detail = OpenCardDetail(current, comms.transcripts.turns(unit.name, LastTurns(3)),
                                        tuple(commits))
            cards.append(UnitCard(unit, current, detail))
        orders = tuple(order for card in cards for order in card.orders)
        alarms = tuple(alarm for source in CommanderAlarmSource.members_with(CommanderAlarmSource)
                       for alarm in source.read(comms, commander, orders, now))
        return cls(commander, tuple(sorted(cards, key=lambda card: card.priority)), alarms)


@dataclass(frozen=True)
class ObserveCommander(InterestRequest):
    root: str
    commander: ThreadIncarnation
    expanded: frozenset[ThreadIncarnation] = frozenset()

    def observation(self) -> CommanderReading:
        return CommanderReading(self)


@dataclass(frozen=True)
class ReadCommanderIdentity(ReadRequest):
    """Resolve the external command spelling once, in the existing read process."""
    root: str
    name: str

    def read(self, comms: Comms) -> ObserveCommander:
        return ObserveCommander(self.root, comms.registry.require(self.name).incarnation)


class CommanderReading(Observation[ObserveCommander]):
    def __init__(self, request: ObserveCommander):
        super().__init__(request)
        self.revision: ReadState = NoRevision()

    def read(self, comms: Comms, revision: WireRevision) -> Iterator[ObservationStep]:
        if revision.changed_since(CommanderView(), self.revision):
            model = UnreadableHistory.outcome(lambda: CommanderModel.read(comms, self.request))
            # Contention is retried on the next observer turn, not published as an empty board.
            ready = model.answer(lambda value: True, lambda: False, lambda error: True)
            if ready:
                self.revision = revision
                yield CommanderObserved(revision, self.request, model)


@dataclass(frozen=True)
class CommanderObserved(Observed):
    request: ObserveCommander
    model: StoredRead[CommanderModel]


@dataclass(frozen=True)
class StopCommander(ObservationRequest):
    def receive(self, service: ObservationService) -> None:
        service.interest.remove(CommanderReading)
