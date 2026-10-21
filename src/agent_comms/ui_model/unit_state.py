"""Unit cards and decisions, derived from the original Core records.

This is a subfamily of StandingRule, not a second classification registry. The
inheritance chain declares urgency: a more urgent state is a subtype of every
less urgent state. Readers use that MRO, never a separately numbered priority.
"""
from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from functools import cached_property
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.bus_page_index import BusPageIndex
from agent_comms.declared_family import DeclaredFamily
from agent_comms.goal_actions import GoalPrecondition
from agent_comms.goal_generation import GenerationState, RetryableGeneration
from agent_comms.goal_states import BlockedState
from agent_comms.message_page import MessageReadRequest
from agent_comms.messages import Message, NeedsAnswerKind
from agent_comms.read_basis import RepliesDisplayScope
from agent_comms.standing_rules import StandingRule, UndatedStandingRule
from agent_comms.thread_identity import CoreWatcherIncarnation, ThreadIncarnation
from agent_comms.todos import (
    OrderProgress, ProgressSource, PullRequests, SendBackVerdict, Todo,
)
from agent_comms.transcript_events import OrderCall, TurnDigest
from agent_comms.turn_digests import LastTurns, TurnDigestRead
from agent_comms.wire_log import WireSnapshot

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.goals import Goal
    from agent_comms.message_reference import MessageReference
    from agent_comms.threads import Thread


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
                elif (issubclass(message.type, NeedsAnswerKind)
                      and message.timestamp <= self.before and message.reference not in answered):
                    found.append(message)
        return tuple(found)


@dataclass(frozen=True)
class UnitObservation:
    """One bounded read shared by all candidate states; never retained across reads."""

    comms: Comms
    thread: Thread
    todos: tuple[Todo, ...]
    pulls: PullRequests
    questions: tuple[Message, ...]
    now: float

    @cached_property
    def pending(self) -> tuple[Todo, ...]:
        return tuple(todo for todo in self.todos if todo.pending)

    @cached_property
    def turns(self) -> tuple[TurnDigest, ...]:
        return TurnDigestRead(self.comms.root, self.thread, LastTurns(2)).read().turns

    @cached_property
    def calls(self) -> tuple[OrderCall, ...]:
        calls: dict[str, OrderCall] = {}
        for turn in reversed(self.turns):
            for event in turn.events:
                event.record_call(calls, self.since)
        return tuple(calls.values())

    @cached_property
    def since(self) -> float:
        return min(todo.order.checked().at.recorded_at("A held order") for todo in self.pending)

    @cached_property
    def sources(self) -> tuple[ProgressSource, ...]:
        return tuple(ProgressSource.of(todo, self.comms.registry, self.pulls) for todo in self.todos)

    @cached_property
    def progress(self) -> tuple[OrderProgress, ...]:
        return tuple(OrderProgress.from_source(source) for source in self.sources)

    @property
    def orders(self) -> tuple[str, ...]:
        return tuple(todo.id for todo in self.todos)


@dataclass(frozen=True)
class UnitState(UndatedStandingRule, declared_name=None):
    """One selected state per unit. Urgency is the states' declared MRO."""

    unit: ThreadIncarnation
    orders: tuple[str, ...]
    action: ClassVar[str]

    @classmethod
    def worst_first(cls) -> tuple[type[UnitState], ...]:
        members = UnitState.members_with(UnitState)
        most_urgent = max(members, key=lambda member: len(member.__mro__))
        ordered = tuple(base for base in most_urgent.__mro__ if base in members)
        if len(ordered) != len(members):
            raise TypeError("UnitState urgency must be declared by one complete MRO")
        return ordered

    @classmethod
    def scan_owner(cls) -> type[StandingRule]:
        return UnitState

    @classmethod
    def holders(cls, comms: Comms, commander: str) -> dict[ThreadIncarnation, list[Todo]]:
        held: dict[ThreadIncarnation, list[Todo]] = {}
        for todo in StandingRule.todos(comms, commander):
            held.setdefault(todo.held().owner_identity, []).append(todo)
        return held

    @classmethod
    def asks(cls, comms: Comms, commander: str, before: float) -> tuple[Message, ...]:
        held = cls.holders(comms, commander)
        pending = tuple(todo for todos in held.values() for todo in todos if todo.pending)
        if not pending:
            return ()
        snapshot = comms.registry.snapshot()
        peers = frozenset(name for owner in held for name in snapshot.names_for(owner.name))
        since = min(todo.order.checked().at.recorded_at("A held order") for todo in pending)
        return comms.bus.log.read_page(UnansweredUnitAsks(
            RepliesDisplayScope(snapshot.names_for(commander), peers), since, before,
        ))

    @classmethod
    def read(cls, observation: UnitObservation) -> UnitState:
        return next(state for member in cls.worst_first() for state in member.derive(observation))

    @classmethod
    def observations(cls, comms: Comms, commander: str,
                     pulls: PullRequests | None = None) -> tuple[UnitObservation, ...]:
        snapshot, now = comms.registry.snapshot(), time.time()
        held = cls.holders(comms, commander)
        for owner in held:
            if snapshot.require(owner.name).incarnation != owner:
                raise ValueError("Held order owner incarnation changed")
        questions = cls.asks(comms, commander, now - AwaitingCommander.seconds)
        requests = pulls if pulls is not None else PullRequests(tuple(
            todo for todos in held.values() for todo in todos))
        found = []
        for thread in snapshot.threads.values():
            todos = tuple(held.get(thread.incarnation, ()))
            if todos or thread.role.executor(thread) and thread.name != commander:
                found.append(UnitObservation(comms, thread, todos, requests,
                    tuple(message for message in questions
                          if snapshot.canonical_name(message.sender) == thread.name), now))
        return tuple(found)

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return tuple(state for source in cls.observations(comms, commander)
                     for state in (UnitState.read(source),)
                     if (cls is UnitState or type(state) is cls) and state.attention())

    @classmethod
    @abstractmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]: ...

    def unit_states(self) -> tuple[UnitState, ...]:
        return (self,)

    def commander_pings(self, todos, states):
        from agent_comms.commander_pings import OrderRevision, UnitStatePing

        return (UnitStatePing(self.unit, type(self), tuple(
            OrderRevision.of(todos[problem]) for problem in self.orders),
            self.detail(), self.default_action),)

    def attention(self) -> bool:
        return False

    @property
    def default_action(self) -> str:
        return self.action

    def detail(self) -> str:
        return f"orders {', '.join(self.orders)}"

    def line(self) -> str:
        return (f"{self.declared_name.replace('_', ' ').upper()} {self.unit.name}: "
                f"{self.detail()} -> {self.default_action}")

    def more_urgent_than(self, previous: type[UnitState]) -> bool:
        return type(self) is not previous and issubclass(type(self), previous)


class Idle(UnitState):
    action = "give it the next useful order"

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        if not source.todos:
            yield cls(source.thread.incarnation, ())


class Moving(Idle):
    action = "let it continue"

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        if source.todos:
            yield cls(source.thread.incarnation, source.orders)


class DeliveredUnclosed(Moving):
    action = "install the merge or close with the working application evidence"

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        if source.todos and (not source.pending or any(
            progress.waiting_on_install(todo) for todo, progress in zip(source.todos, source.progress)
        )):
            yield cls(source.thread.incarnation, source.orders)

    def attention(self) -> bool:
        return True


class StalledOnNothing(DeliveredUnclosed):
    action = "resume its work or give it the missing next step"

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        if (source.pending and source.turns and not source.thread.executing
                and not source.comms.bus.pending_count(source.thread.name)
                and not source.comms.goals.goal_execution(source.thread.name).standby()):
            yield cls(source.thread.incarnation, source.orders)


class Spinning(StalledOnNothing):
    action = "read the failed approach and change it before another rerun"

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        if not source.pending:
            return
        sends = sorted(verdict.at.recorded_at("A send-back")
                       for todo in source.pending for verdict in todo.verdicts
                       if isinstance(verdict, SendBackVerdict))
        commit_at = max((commit.at for origin in source.sources for commit in origin.commits()),
                        default=source.since)
        calls = source.calls
        repeats = len(calls) >= 2 and calls[-1].digest == calls[-2].digest
        repeated_after_commit = repeats and source.turns[-1].start.since(commit_at)
        if (len(sends) >= 2 and sends[-2] > commit_at) or repeated_after_commit:
            yield cls(source.thread.incarnation, source.orders)


class Stuck(Spinning):
    seconds: ClassVar[int] = 20 * 60
    action = "inspect its last turn and restart only known-safe work"

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        if not source.pending:
            return
        calls = source.calls
        latest = max((event.timestamp for turn in source.turns for event in turn.events),
                     default=source.since)
        if (not calls and any(todo.order.checked().first_call_deadline.before(source.now)
                              for todo in source.pending)) or latest < source.now - cls.seconds:
            yield cls(source.thread.incarnation, source.orders)


@dataclass(frozen=True)
class AwaitingCommander(Stuck):
    questions: tuple[Message, ...] = ()
    seconds: ClassVar[int] = 15 * 60
    action = "answer the original message"

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        if source.pending and source.questions:
            yield cls(source.thread.incarnation, source.orders, source.questions)

    def detail(self) -> str:
        return ", ".join(f"message {message.reference.seq}:{message.message_id}"
                         for message in self.questions)


@dataclass(frozen=True)
class Detached(AwaitingCommander):
    unread: int = 0
    action = "wake it or reassign the order"

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        if source.pending and not source.comms.registry.status(source.thread.name).receiving:
            yield cls(source.thread.incarnation, source.orders,
                      unread=source.comms.bus.pending_count(source.thread.name))

    def detail(self) -> str:
        return f"{self.unread} unread; {UnitState.detail(self)}"


@dataclass(frozen=True)
class UnitBlock(DeclaredFamily, affix="UnitBlock"):
    goal: Goal
    action: ClassVar[str]

    @staticmethod
    @abstractmethod
    def accepts(goal: Goal, generation: GenerationState) -> bool: ...

    def enforce(self, comms: Comms, commander: str, unit: str, orders: tuple[str, ...]) -> None:
        """Unknown attempts never run again merely because the connector restarted."""


class UnresolvedUnitBlock(UnitBlock):
    action = "inspect the unknown attempt, then fail or finish it; never replay its input"

    @staticmethod
    def accepts(goal: Goal, generation: GenerationState) -> bool:
        return not isinstance(generation, RetryableGeneration)


class RetryableUnitBlock(UnitBlock):
    action = "reissue only the known-safe orders"

    @staticmethod
    def accepts(goal: Goal, generation: GenerationState) -> bool:
        return isinstance(goal.state, BlockedState) and isinstance(generation, RetryableGeneration)

    def enforce(self, comms: Comms, commander: str, unit: str, orders: tuple[str, ...]) -> None:
        from agent_comms.commander.standing import UnownedWheelSpin

        observed = self.goal
        for problem in orders:
            todo = comms.goals.todos.get(problem)
            todo.require_assignee(comms.registry.require(unit).incarnation, observed.id)
            action = UnownedWheelSpin.order_for(todo, commander, CoreWatcherIncarnation.watcher())
            observed = comms.goals.update_goal(unit, replace(action, expect=GoalPrecondition(
                goal_id=observed.id, expected_state=observed.state,
                expected_checkpoint=observed.captured,
            )))


@dataclass(frozen=True)
class Blocked(Detached):
    blocks: tuple[UnitBlock, ...] = ()

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        if not source.pending or source.thread.executing:
            return
        store = source.comms.goals.open_goal_store()
        blocks = tuple(member(goal) for goal in source.thread.goal
                       for generation in (store.require_generation(goal.id).lifecycle,)
                       for member in UnitBlock.members_with(UnitBlock)
                       if member.accepts(goal, generation))
        if blocks:
            yield cls(source.thread.incarnation, tuple(todo.id for todo in source.pending), blocks=blocks)

    @property
    def default_action(self) -> str:
        return "; ".join(block.action for block in self.blocks)

    def detail(self) -> str:
        goals = ", ".join(f"goal {block.goal.id}@{block.goal.revision}" for block in self.blocks)
        return f"{UnitState.detail(self)}; {goals}"

    def enforce(self, comms: Comms, commander: str) -> None:
        for block in self.blocks:
            block.enforce(comms, commander, self.unit.name, self.orders)
