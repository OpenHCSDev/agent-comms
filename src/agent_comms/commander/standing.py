"""Standing rules read from the commander's own files, members of Core's StandingRule family."""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from abc import abstractmethod
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar, Self

from agent_comms.build_identity import RecordedBuildIdentity
from agent_comms.comms import Comms
from agent_comms.goal_actions import GoalPrecondition, ReissuedOrderGoalAction
from agent_comms.goals import Goal
from agent_comms.goal_states import BlockedState
from agent_comms.goal_history import GoalHistoryStore
from agent_comms.goal_generation import GenerationState, RetryableGeneration
from agent_comms.message_page import MessageReadRequest
from agent_comms.message_reference import MessageReference
from agent_comms.messages import Message
from agent_comms.read_basis import RepliesDisplayScope
from agent_comms.bus_page_index import BusPageIndex
from agent_comms.wire_log import WireSnapshot
from agent_comms.message_bus import WatchPass
from agent_comms.standing_rules import ReasonedStandingRule, StandingRule, UndatedStandingRule
from agent_comms.storage_rules import OrderCheckoutWork
from agent_comms.thread_identity import CoreWatcherIncarnation, ThreadIncarnation
from agent_comms.threads import Thread
from agent_comms.todos import Todo
from agent_comms.turn_digests import OrderEditCountRead, TurnDigestRead, TurnsSince
from agent_comms.work_dispatch import ReusedWorkDispatch, WorkDispatch
from agent_comms.commander.config import CommanderConfig


@dataclass(frozen=True)
class InstallDue(UndatedStandingRule):
    """Each configured main not contained in the installed build needs an install."""

    why: str

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        config = CommanderConfig.read_root(comms.root)
        installed = RecordedBuildIdentity.installed()
        due: list[Self] = []
        for repo in config.repos:
            repo.git(repo.checkout, "fetch", "-q", "origin", "main")
            main = repo.git(repo.checkout, "rev-parse", "origin/main").strip()
            if not installed.contains(main, repo.checkout):
                recorded = installed.for_repository(repo)
                due.append(cls(
                    f"{repo.github.to_wire()}: main {main}, installed {recorded.commit}"))
        return tuple(due)

    def line(self) -> str:
        return (f"INSTALL DUE ({self.why}) -> the installer rehearses and installs main with "
                "agent-comms install and posts the result to the commander")


@dataclass(frozen=True)
class HolderCannotRun(UndatedStandingRule, declared_name=None):
    unit: str
    orders: tuple[str, ...]

    @staticmethod
    def holders(comms: Comms, commander: str) -> dict[ThreadIncarnation, list[Todo]]:
        held: dict[ThreadIncarnation, list[Todo]] = {}
        for todo in StandingRule.pending_todos(comms, commander):
            held.setdefault(todo.held().owner_identity, []).append(todo)
        return held

    @classmethod
    def scan_owner(cls) -> type[StandingRule]:
        return HolderCannotRun

    @staticmethod
    @abstractmethod
    def stalled(comms: Comms, thread: Thread) -> bool: ...

    @classmethod
    @abstractmethod
    def found(cls, comms: Comms, thread: Thread, orders: tuple[str, ...]) -> Self: ...

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        registry = comms.registry.snapshot()
        found: list[Self] = []
        members = cls.members_with(cls)
        for owner, todos in cls.holders(comms, commander).items():
            thread = registry.require(owner.name)
            if thread.incarnation != owner:
                raise ValueError("Held order owner incarnation changed")
            orders = tuple(todo.id for todo in todos)
            found.extend(member.found(comms, thread, orders)
                         for member in members if member.stalled(comms, thread))
        return tuple(found)


@dataclass(frozen=True)
class Detached(HolderCannotRun):
    unread: int = field(compare=False)

    @staticmethod
    def stalled(comms: Comms, thread: Thread) -> bool:
        return not comms.registry.status(thread.name).receiving

    @classmethod
    def found(cls, comms: Comms, thread: Thread, orders: tuple[str, ...]) -> Self:
        return cls(thread.name, orders, comms.bus.pending_count(thread.name))

    def line(self) -> str:
        return (f"DETACHED {self.unit}: {self.unread} unread, orders {', '.join(self.orders)}"
                " -> wake it or reassign")


@dataclass(frozen=True)
class BlockedHolder(HolderCannotRun, declared_name=None):
    """An idle holder classified by its goal and original attempt ledger."""

    block_record: Path
    goal: Goal

    @staticmethod
    @abstractmethod
    def accepts(goal: Goal, generation: GenerationState) -> bool: ...

    @classmethod
    def stalled(cls, comms: Comms, thread: Thread) -> bool:
        store = comms.goals.open_goal_store()
        return not thread.executing and any(
            cls.accepts(goal, store.require_generation(goal.id).lifecycle)
            for goal in thread.goal)

    @classmethod
    def found(cls, comms: Comms, thread: Thread, orders: tuple[str, ...]) -> Self:
        return cls(
            thread.name, orders, GoalHistoryStore.path_in(comms.root), thread.goal.require()
        )


@dataclass(frozen=True)
class UnresolvedBlock(BlockedHolder):
    """An attempt with an unknown outcome: Core never retries it, so it is never replayed."""

    @staticmethod
    def accepts(goal: Goal, generation: GenerationState) -> bool:
        return not isinstance(generation, RetryableGeneration)

    def line(self) -> str:
        return (f"UNRESOLVED BLOCK {self.unit}: orders {', '.join(self.orders)}; "
                f"goal {self.goal.id}@{self.goal.revision} has an attempt with an unknown result "
                "-> inspect it, then fail or finish the attempt")


@dataclass(frozen=True)
class Blocked(BlockedHolder):
    @staticmethod
    def accepts(goal: Goal, generation: GenerationState) -> bool:
        return isinstance(goal.state, BlockedState) and isinstance(generation, RetryableGeneration)

    def enforce(self, comms: Comms, commander: str) -> None:
        observed = self.goal
        for problem in self.orders:
            todo = comms.goals.todos.get(problem)
            todo.require_assignee(comms.registry.require(self.unit).incarnation, observed.id)
            action = UnownedWheelSpin.order_for(todo, commander, CoreWatcherIncarnation.watcher())
            observed = comms.goals.update_goal(self.unit, replace(
                action, expect=GoalPrecondition(
                    goal_id=observed.id, expected_state=observed.state,
                    expected_checkpoint=observed.captured,
                ),
            ))

    def line(self) -> str:
        return (f"BLOCKED {self.unit}: orders {', '.join(self.orders)}; "
                f"block record {self.block_record}, goal {self.goal.id}@{self.goal.revision} "
                "-> Core reissues the orders")


@dataclass(frozen=True)
class RequestStalled(UndatedStandingRule):
    goal: Goal

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[RequestStalled, ...]:
        thread = comms.registry.require(commander)
        return tuple(cls(request) for goal in thread.goal.active()
                     for request, served in goal.order.open_requests(
                         goal, comms.goals, thread.incarnation)
                     if not served)

    def line(self) -> str:
        return (f"REQUEST STALLED: {self.goal.id} {self.goal.text}"
                " -> order its next step or mark it done")


@dataclass(frozen=True, slots=True)
class UnansweredUnitAsks(MessageReadRequest[tuple[Message, ...]]):
    scope: RepliesDisplayScope
    since: float
    before: float
    empty: ClassVar[tuple[Message, ...]] = ()

    def indexed(self, index: BusPageIndex, source: WireSnapshot) -> tuple[Message, ...]:
        answered: set[MessageReference] = set()
        found: list[Message] = []
        with closing(index.recent_offsets(
            self.since, predicate=self.scope.index_predicate, before_offset=source.boundary,
        )) as offsets:
            for row in self.indexed_records(source, offsets):
                message = row.message
                if message.sender in self.scope.first_names:
                    answered.update(message.answers)
                elif message.timestamp <= self.before and message.reference not in answered:
                    found.append(message)
        return tuple(found)


@dataclass(frozen=True)
class UnitAskUnanswered(UndatedStandingRule):
    unit: str
    reference: MessageReference
    sent_at: float
    seconds: ClassVar[int] = 15 * 60

    @staticmethod
    def asks(comms: Comms, commander: str, before: float) -> tuple[Message, ...]:
        """Holders' messages to the commander, sent by ``before``, that no commander reply answers."""
        held = HolderCannotRun.holders(comms, commander)
        if not held:
            return ()
        snapshot = comms.registry.snapshot()
        peers = frozenset(name for owner in held for name in snapshot.names_for(owner.name))
        since = min(todo.order.checked().at.recorded_at("A held order")
                    for todos in held.values() for todo in todos)
        return comms.bus.log.read_page(UnansweredUnitAsks(
            RepliesDisplayScope(snapshot.names_for(commander), peers), since, before,
        ))

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        snapshot = comms.registry.snapshot()
        return tuple(cls(snapshot.canonical_name(message.sender), message.reference, message.timestamp)
                     for message in cls.asks(comms, commander, time.time() - cls.seconds))

    def line(self) -> str:
        age = max(0, int((time.time() - self.sent_at) // 60))
        return (f"UNANSWERED {self.unit}: message {self.reference.seq}:{self.reference.message_id}, "
                f"{age} min old -> commander must answer")


@dataclass(frozen=True)
class UnownedWheelSpin(UndatedStandingRule):
    """A unit holds no next order while the board has unowned rows; Core hands the row to it
    without a commander turn, restarting the row's review window at the handoff."""

    todo: Todo

    recent: ClassVar[int] = 10 * 60

    @classmethod
    def idle(cls, comms: Comms, commander: str) -> list[Thread]:
        """Units with a recent turn in their own transcript and no order, parked threads aside."""
        held = {todo.holder for todo in cls.todos(comms, commander)}
        excluded = held | CommanderConfig.read_root(comms.root).parked | {commander, ""}
        bound = TurnsSince(time.time() - cls.recent)
        return sorted((thread for thread in comms.registry.all_threads().values()
                       if thread.name not in excluded
                       and TurnDigestRead(comms.root, thread, bound).read().turns),
                      key=lambda thread: thread.name)

    @staticmethod
    def order_for(todo: Todo, commander: str, assigner: ThreadIncarnation) -> ReissuedOrderGoalAction:
        now = time.time()
        return ReissuedOrderGoalAction(
            problem=todo.id, expected_revision=todo.revision,
            order=todo.order.reissued(now), request=todo.goal.request(),
            review_at=todo.reissued_review(now), commander=commander, assigner=assigner,
            traced=todo.order.owner)

    def _identity(self) -> tuple[object, ...]:
        # Observation times and proposed idle units change; the row and its disposition do not.
        return type(self), self.todo.id

    def __eq__(self, other: object) -> bool:
        return isinstance(other, UnownedWheelSpin) and self._identity() == other._identity()

    def __hash__(self) -> int:
        return hash(self._identity())

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[UnownedWheelSpin, ...]:
        actor = comms.registry.require(commander).incarnation
        rows = tuple(todo for todo in comms.goals.todos.for_commander(actor)
                     if not todo.holder_state.current_assignments())
        return cls.paired(comms, commander, rows, actor)

    @classmethod
    def paired(cls, comms: Comms, commander: str, todos: Iterable[Todo],
               assigner: ThreadIncarnation) -> tuple[UnownedWheelSpin, ...]:
        """Both the heartbeat and automatic watcher preserve work before any dispatch."""
        rows = tuple(todo for todo in todos if todo.pending)
        retained = tuple(CheckoutHeldWheelSpin(todo, work)
                         for todo in rows
                         for work in comms.registry.storage_rule.work_for_problem(
                             todo, comms.registry, comms.goals.todos))
        protected = {rule.todo.id for rule in retained}
        idle, found = cls.idle(comms, commander), []
        for todo in (todo for todo in rows if todo.id not in protected):
            for unit in idle:
                if not todo.live_strikes(unit.name):
                    idle.remove(unit)
                    found.append(ReassignedWheelSpin(todo, ReusedWorkDispatch.for_unit(
                        cls.order_for(todo, commander, assigner), unit)))
                    break
        covered = {rule.todo.id for rule in (*retained, *found)}
        return (*retained, *found,
                *(WaitingUnownedWheelSpin(todo) for todo in rows if todo.id not in covered))

    def line(self) -> str:
        return f"UNOWNED ROW: {self.todo.id} waits for a new unit"


@dataclass(frozen=True, eq=False)
class ReassignedWheelSpin(UnownedWheelSpin, declared_name=None):
    dispatch: WorkDispatch

    @property
    def unit(self) -> str:
        return self.dispatch.name

    def enforce(self, comms: Comms, commander: str) -> None:
        unit = self.dispatch.start(comms)
        comms.messaging.send(commander, unit.name,
            f"{self.line()}\n{self.dispatch.action.order.instructions(self.todo)}")

    def line(self) -> str:
        return (f"WHEEL SPIN: {self.unit} holds no order while {self.todo.id} is unowned"
                f" -> Core gives it to {self.unit}")


@dataclass(frozen=True, eq=False)
class CheckoutHeldWheelSpin(UnownedWheelSpin, declared_name=None):
    work: OrderCheckoutWork

    def _identity(self) -> tuple[object, ...]:
        return (*super()._identity(), self.work.owner.incarnation, self.work.checkout)

    def line(self) -> str:
        return (f"WORK HELD: {self.todo.id} {self.work.owner.name} retains work in "
                f"{self.work.checkout} -> restore its holder; do not hand the row away")


@dataclass(frozen=True)
class UnownedObservation:
    commander: Thread
    rule: UnownedWheelSpin


class UnownedOrderWatch(WatchPass[tuple[UnownedObservation, ...]]):
    """Observe released work and its checkouts before entering the bus to assign it."""

    def observe(self, comms: Comms) -> tuple[UnownedObservation, ...]:
        available = comms.goals.todos.reassignable()
        observed: list[UnownedObservation] = []
        for name in {todo.creator_identity.name for todo in available}:
            commander = comms.registry.require(name)
            mine = (todo for todo in available if todo.creator_identity.name == name)
            observed.extend(
                UnownedObservation(commander, rule)
                for rule in UnownedWheelSpin.paired(
                    comms, name, mine, CoreWatcherIncarnation.watcher()
                )
            )
        return tuple(observed)

    def pass_body(self, comms: Comms, observed: tuple[UnownedObservation, ...]) -> None:
        for item in observed:
            StandingRule.enforce_all(comms, item.commander.name, (item.rule,))


class WaitingUnownedWheelSpin(UnownedWheelSpin, declared_name=None):
    """A row needs a new unit; a proposed dispatch name is not an idle thread."""

    def heartbeat_group(self) -> object:
        return type(self)

    def heartbeat_line(self, current: Sequence[WaitingUnownedWheelSpin]) -> str:
        rows = ", ".join(sorted(rule.todo.id for rule in current))
        return f"{len(current)} unowned rows wait for a new unit: {rows}"


@dataclass(frozen=True)
class Unjudged(ReasonedStandingRule, UndatedStandingRule):
    limit: ClassVar[int] = 30
    text = "UNJUDGED {why} edits -> judge or fork a judge slice"

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        total = OrderEditCountRead.count(comms, commander)
        return str(total) if total > cls.limit else None
