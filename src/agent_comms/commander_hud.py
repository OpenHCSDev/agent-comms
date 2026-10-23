"""Commander display derived from the native record owners in one read."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.vibe_check import OverloadDegradation
from agent_comms.field_codec import projected
from agent_comms.plan_phases import PlanPhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.todos import OrderProgress, ProgressRead, PullRequest, Todo, UnmergedListedPullRequest
from agent_comms.tool_result import ToolResult
from agent_comms.turn_digests import OrderEditCountRead

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True)
class CommanderOrder:
    """Small order card; identifiers/text come from Todo, review rounds from TodoStore."""

    problem: str
    text: str
    order: str
    state: str
    review_timers_up: tuple[RecordedEventTime, ...]
    strikes: int
    unjudged_edits: int


@dataclass(frozen=True)
class CommanderUnit:
    """One holder's orders, live strikes and GitHub's open ready/draft requests."""

    unit: ThreadIncarnation
    orders: tuple[CommanderOrder, ...]
    strikes: int
    ready_prs: tuple[PullRequest, ...]
    draft_prs: tuple[PullRequest, ...]

    def line(self) -> str:
        orders = "; ".join(f"{order.problem}: {order.state}" for order in self.orders)
        return (
            f"{self.unit.name}: orders={orders} "
            f"review timers={sum(len(order.review_timers_up) for order in self.orders)} "
            f"strikes={self.strikes} edits={sum(order.unjudged_edits for order in self.orders)} "
            f"ready={','.join(str(pr.number) for pr in self.ready_prs)} "
            f"draft={','.join(str(pr.number) for pr in self.draft_prs)}"
        )


@dataclass(frozen=True)
class CommanderPhase:
    """PlanPhase supplies every answer; no HUD status taxonomy is maintained beside it."""

    phase: type[PlanPhase]
    done: bool
    ready: bool
    holders: tuple[ThreadIncarnation, ...]

    def line(self) -> str:
        return (
            f"{self.phase.plan.declared_name}/{self.phase.declared_name} "
            f"done={self.done} ready={self.ready} "
            f"holders={','.join(unit.name for unit in self.holders)}"
        )


@dataclass(frozen=True)
class CommanderHud(ToolResult):
    """One disposable view of native records, shared by comms_hud and the commander board.

    TodoStore owns orders, review timers and strikes; OrderEditCountRead owns edit counts;
    PullRequests owns ready/draft facts; PlanPhase owns phase state; OverloadDegradation
    owns the load formula and decision. Nothing here writes or retains a board.
    """

    commander: ThreadIncarnation
    units: tuple[CommanderUnit, ...]
    phases: tuple[CommanderPhase, ...]
    load: OverloadDegradation

    @classmethod
    def read(cls, comms: Comms, commander: str) -> CommanderHud:
        snapshot, now = comms.registry.snapshot(), time.time()
        owner = snapshot.require(commander).incarnation
        store = comms.goals.todos
        history = store.history_for_commander(owner)
        held = tuple(todo for todo in history if todo.holder)
        pulls = ProgressRead(held)
        progress = {todo.id: OrderProgress.read(todo, comms.registry, pulls) for todo in held}
        timers = store.timers_up_in(held, now, progress)
        edits = {
            read.todo.id: read.read()
            for read in OrderEditCountRead.for_orders(comms.root, snapshot, held)
        }
        grouped: dict[ThreadIncarnation, list[Todo]] = {}
        for todo in held:
            grouped.setdefault(todo.held().owner_identity, []).append(todo)
        units = []
        for unit, orders in grouped.items():
            requests = {
                (todo.repo, request.number): request
                for todo in orders
                for request in pulls.listed(todo.repo, UnmergedListedPullRequest, todo)
            }
            units.append(CommanderUnit(
                unit,
                tuple(CommanderOrder(
                    todo.id, todo.text, todo.order.checked().id, todo.order.line,
                    tuple(at for due, at in timers if due.id == todo.id),
                    todo.strike_count, edits[todo.id],
                ) for todo in orders),
                sum(len(todo.live_strikes(unit.name)) for todo in history),
                tuple(request for request in requests.values() if not request.draft),
                tuple(request for request in requests.values() if request.draft),
            ))
        reading = CommanderConfig.read_root(comms.root).plan_reading(history)
        phases = tuple(CommanderPhase(
            phase, phase.done(reading), phase.ready(reading),
            tuple(dict.fromkeys(todo.held().owner_identity for todo in phase.holding(reading))),
        ) for phase in PlanPhase.members_with(PlanPhase))
        return cls(owner, tuple(units), phases,
                   OverloadDegradation.from_orders(held, len(timers), sum(edits.values())))

    @projected(view="wire")
    def frame(self) -> tuple[str, ...]:
        """One line per declared phase or holder, with their original order and PR identities."""
        return (
            str(self.load),
            *(row.line() for row in self.phases),
            *(row.line() for row in self.units),
        )


