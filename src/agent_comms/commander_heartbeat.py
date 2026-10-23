"""Service-owned commander review: changed standing rules wake through the real inbox."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.vibe_check import OverloadDegradation
from agent_comms.field_codec import projected
from agent_comms.message_bus import WatchPass
from agent_comms.plan_phases import PlanPhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.registration import Registration
from agent_comms.service_sender import ServiceSender
from agent_comms.standing_rules import StandingRule
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.threads import Thread
from agent_comms.todos import ProgressRead, PullRequest, Todo, UnmergedListedPullRequest
from agent_comms.tool_result import ToolResult
from agent_comms.turn_digests import OrderEditCountRead
from agent_comms.ui_model.unit_state import UnitState

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.goal_management import Goals
    from agent_comms.todos import OwnerRequest


@dataclass(frozen=True)
class CommanderOrder:
    """Small order card; identifiers/text come from Todo, review rounds from TodoStore."""

    problem: str
    text: str
    order: str
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


@dataclass(frozen=True)
class CommanderPhase:
    """PlanPhase supplies every answer; no HUD status taxonomy is maintained beside it."""

    phase: type[PlanPhase]
    done: bool
    ready: bool
    holders: tuple[ThreadIncarnation, ...]


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
        timers = store.timers_up_in(held, comms.registry, now, pulls)
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
                    todo.id, todo.text, todo.order.checked().id,
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
            *(f"{row.phase.plan.declared_name}/{row.phase.declared_name} "
              f"done={row.done} ready={row.ready} "
              f"holders={','.join(unit.name for unit in row.holders)}" for row in self.phases),
            *(f"{row.unit.name}: orders={','.join(order.problem for order in row.orders)} "
              f"review timers={sum(len(order.review_timers_up) for order in row.orders)} "
              f"strikes={row.strikes} edits={sum(order.unjudged_edits for order in row.orders)} "
              f"ready={','.join(str(pr.number) for pr in row.ready_prs)} "
              f"draft={','.join(str(pr.number) for pr in row.draft_prs)}" for row in self.units),
        )


@dataclass
class CommanderReview:
    """One commander's handled observations and its last changed beat.

    Seq 0 precedes every published message: no changed beat has been sent yet.
    """

    rules: frozenset[StandingRule]
    states: frozenset[UnitState] = frozenset()
    changed: int = 0

    def wake(self, comms: Comms, sender: str, commander: Thread,
             rules: tuple[StandingRule, ...], states: tuple[UnitState, ...]) -> None:
        """Every pass beats, so silence is a failure; an unchanged beat is one short line."""
        found_rules, found_states = frozenset(rules), frozenset(states)
        actionable = (*rules, *(state for state in states if state.attention()))
        old_rules = self.rules
        previous = self.rules | self.states
        if found_rules == self.rules and found_states == self.states and self.changed:
            comms.messaging.send(sender, commander.name,
                                 f"COMMANDER HEARTBEAT: unchanged since seq {self.changed},"
                                 f" {len(actionable)} violations.")
            return
        # Mark the observation before any effect; an uncertain publication is not replayed.
        self.rules, self.states = found_rules, found_states
        rejected = StandingRule.enforce_all(
            comms, commander.name, (rule for rule in actionable if rule not in previous)
        )
        # A checked rejection has no effects and must not suppress a fresh observation.
        self.rules -= rejected
        self.states -= rejected
        lines = ["COMMANDER HEARTBEAT: unit states and standing rules changed."]
        lines.extend(state.line() for state in states)
        lines.extend(StandingRule.heartbeat_changes(old_rules, self.rules))
        lines.extend(rule.rejected_line() for rule in actionable if rule in rejected)
        if not actionable:
            lines.append("Standing rules clear.")
        self.changed = comms.messaging.send(sender, commander.name, "\n".join(lines)).message.seq


@dataclass(frozen=True)
class CommanderObservation:
    thread: Thread
    merged: tuple[Todo, ...]
    rules: tuple[StandingRule, ...]
    states: tuple[UnitState, ...]

    @classmethod
    def read(cls, comms: Comms, thread: Thread) -> CommanderObservation:
        pulls = ProgressRead(StandingRule.todos(comms, thread.name))
        states = UnitState.current(comms, thread.name, pulls)
        rules = StandingRule.broken(comms, thread.name, observed=states)
        merged = tuple(todo for state in states for todo in state.merged_orders(comms, pulls))
        return cls(thread, merged, rules, states)


@dataclass
class CommanderHeartbeat(ServiceSender, WatchPass[tuple[CommanderObservation, ...]]):
    sender = "commander-heartbeat"
    reviews: dict[ThreadIncarnation, CommanderReview] = field(default_factory=dict)

    @classmethod
    def requested_by(cls, thread: Thread, goals: Goals) -> tuple[OwnerRequest, ...]:
        """The oldest open request makes a live holder owed service, even before its first beat."""
        return (goals.todos.open_requests(thread.incarnation, limit=1, descending=False)
                if thread.process_alive else ())

    @classmethod
    def commanders(cls, registry: Registration, goals: Goals) -> tuple[Thread, ...]:
        """The live request holders whose current incarnation owns commander work."""
        return tuple(
            thread
            for thread in registry.all_threads().values()
            if cls.requested_by(thread, goals)
        )

    def observe(self, comms: Comms) -> tuple[CommanderObservation, ...]:
        return tuple(
            CommanderObservation.read(comms, thread)
            for thread in self.commanders(comms.registry, comms.goals)
        )

    def pass_body(self, comms: Comms, observed: tuple[CommanderObservation, ...]) -> None:
        """Inactive commanders retire their review after the pass enters the bus."""
        self.register(comms.registry)
        live = {item.thread.incarnation for item in observed}
        self.reviews = {
            owner: review for owner, review in self.reviews.items() if owner in live
        }
        for item in observed:
            comms.goals.todos.record_merges(item.merged)
            review = self.reviews.setdefault(item.thread.incarnation, CommanderReview(frozenset()))
            review.wake(comms, self.sender, item.thread, item.rules, item.states)
