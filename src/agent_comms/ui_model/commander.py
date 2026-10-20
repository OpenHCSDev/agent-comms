"""Commander cards prepared by Core from its existing order and progress owners."""
from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.presentation import NoRevision, ReadState, WireRevision
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.revision_views import CommanderView
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.todos import (
    MeasuredProgressSource,
    OrderProgress,
    ProgressPoint,
    PullRequests,
    PushedProgress,
    Todo,
)
from agent_comms.turn_digests import LastTurns, ThreadTurns
from agent_comms.ui_model.observation import (
    InterestRequest,
    Observation,
    ObservationRequest,
    ObservationStep,
    Observed,
    ReadRequest,
    StoredRead,
    UnreadableHistory,
)

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.ui_model.observation import ObservationService


@dataclass(frozen=True)
class CommanderOrder:
    todo: Todo
    progress: OrderProgress
    overdue: bool
    published: tuple[ProgressPoint, ...]
    pushes: tuple[PushedProgress, ...]

    @property
    def line(self) -> str:
        return (
            f"{'OVERDUE ' if self.overdue else ''}{self.todo.id}: "
            f"{' '.join(self.todo.text.split())} | due {self.todo.deadline.clock()} | "
            f"{self.todo.order.line}; {self.progress.line}"
        )

    @property
    def cadence(self) -> str:
        """Display actual publication clocks, never local-commit clocks or a stored forecast."""
        times = sorted({push.at for push in self.pushes})
        if len(times) < 2:
            return f"{self.todo.id}: cadence unknown (need two recorded pushes)"
        return (
            f"{self.todo.id}: {(times[-1] - times[-2]) / 60:.1f}m/push; "
            f"last push {RecordedEventTime(times[-1]).clock()}"
        )


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

    @property
    def text(self) -> str:
        lines = [
            order.todo.order.instructions(order.todo) + "\nLatest work: " + order.progress.line
            for order in self.orders
        ]
        lines.append("Published commits:\n" + "\n".join(
            f"{order.todo.id}: {point.sha}" for order in self.orders for point in order.published
        ))
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
        return (
            not any(order.overdue for order in self.orders),
            min(order.todo.deadline.recorded_at("An order deadline") for order in self.orders),
            self.unit.name.casefold(),
        )


@dataclass(frozen=True)
class CommanderModel:
    commander: ThreadIncarnation
    units: tuple[UnitCard, ...]
    alarms: tuple[str, ...]

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
            source = MeasuredProgressSource.of(todo, storage, pulls)
            grouped.setdefault(todo.held().owner_identity, []).append(CommanderOrder(
                todo, OrderProgress.from_source(source), todo.overdue(now),
                tuple(source.published()), source.pushes,
            ))
        cards = []
        for unit, orders in grouped.items():
            if comms.registry.require(unit.name).incarnation != unit:
                raise ValueError(f"Order holder {unit.name} changed incarnation; refresh its assignment.")
            current = tuple(orders)
            detail: CardDetail = CardDetail()
            if unit in request.expanded:
                detail = OpenCardDetail(current, comms.transcripts.turns(unit.name, LastTurns(3)))
            cards.append(UnitCard(unit, current, detail))
        alarms = tuple(
            f"STALLED {order.todo.held().owner} {order.todo.id}: {order.progress.line}"
            for card in cards for order in card.orders
            if order.todo.pending and order.progress.stalled(order.todo.order.checked().at.recorded_at("An order"), now)
        )
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
            if model.answer(lambda value: True, lambda: False, lambda error: True):
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
