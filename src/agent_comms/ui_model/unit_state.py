"""One derived card per unit, ordered by the family's declaration order.

States are siblings, not increasingly urgent subtypes of one another. The
StandingRule registry supplies both membership and urgency; no second roster
or independently writable unit status is retained.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from subprocess import TimeoutExpired
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.commander.questions import UnansweredUnitAsks
from agent_comms.field_codec import MalformedRecord
from agent_comms.goal_presentation import GoalSnapshot
from agent_comms.messages import Message
from agent_comms.native_failures import NativePiUnavailable
from agent_comms.repo import GitCommandError
from agent_comms.standing_rules import StandingRule, UndatedStandingRule
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.thread_status import ThreadStatus
from agent_comms.todos import (
    MergedProgress,
    NoCommitProgress,
    OrderProgress,
    ProgressRead,
    ProgressSource,
    Todo,
)
from agent_comms.transcript_events import OrderCall, TurnDigest
from agent_comms.turn_digests import LastTurns, TurnDigestRead
from agent_comms.work_branches import WorkBranchError

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.threads import Thread


@dataclass(frozen=True)
class UnitObservation:
    """Original sources shared by candidate states within this one read."""

    comms: Comms
    thread: Thread
    todos: tuple[Todo, ...]
    pulls: ProgressRead
    questions: tuple[Message, ...]
    now: float
    presence: ThreadStatus
    working: bool

    @cached_property
    def goal(self) -> GoalSnapshot:
        return self.comms.goals.goal_snapshot(self.thread.name)

    @cached_property
    def due_questions(self) -> tuple[Message, ...]:
        return tuple(
            message
            for message in self.questions
            if message.timestamp <= self.now - AwaitingCommander.seconds
        )

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
        return tuple(
            ProgressSource.of(todo, self.comms.registry, self.pulls) for todo in self.todos
        )

    def require_read(self) -> None:
        if self.todos:
            self.pulls.branch_difference(self.thread.project_path)
            for path in self.comms.registry.storage_rule.checkout_paths(self.thread):
                self.pulls.checkout(path).require_read()
        for source in self.sources:
            source.checkout.require_read()

    @property
    def orders(self) -> tuple[str, ...]:
        return tuple(todo.id for todo in self.todos)


class NeedsCommanderAttention:
    """Urgent siblings share their ping behavior without claiming each other's state."""

    def attention(self) -> bool:
        return True


@dataclass(frozen=True)
class UnitState(UndatedStandingRule, declared_name=None):
    unit: ThreadIncarnation
    orders: tuple[str, ...]
    presence: ThreadStatus
    source: UnitObservation = field(compare=False, repr=False)
    action: ClassVar[str]

    @classmethod
    def worst_first(cls) -> tuple[type[UnitState], ...]:
        return UnitState.members_with(UnitState)

    @classmethod
    def scan_owner(cls) -> type[StandingRule]:
        return UnitState

    @classmethod
    def scan_observed(
        cls, comms: Comms, commander: str, observed: Sequence[UnitState]
    ) -> tuple[Self, ...]:
        # States are already read; ordinary scans consume them rather than emit them again.
        return ()

    @classmethod
    def read(cls, source: UnitObservation) -> UnitState:
        try:
            return next(state for member in cls.worst_first() for state in member.derive(source))
        except (
            OSError,
            GitCommandError,
            WorkBranchError,
            TimeoutExpired,
            NativePiUnavailable,
            MalformedRecord,
        ) as failure:
            return Unreadable(
                source.thread.incarnation, source.orders, source.presence, source, str(failure)
            )

    @classmethod
    def observations(
        cls, comms: Comms, commander: str, pulls: ProgressRead
    ) -> tuple[UnitObservation, ...]:
        snapshot, now = comms.registry.snapshot(), time.time()
        held = StandingRule.holders(comms, commander)
        requests = pulls.register(tuple(todo for todos in held.values() for todo in todos))
        with comms.bus.bus.reading() as source:
            questions = UnansweredUnitAsks.for_orders(source, snapshot, commander, held, now)
        readings = tuple(
            UnitObservation(
                comms,
                thread,
                tuple(held.get(thread.incarnation, ())),
                requests,
                questions.get(thread.incarnation, ()),
                now,
                snapshot.status(thread.name),
                comms.agents.is_working(thread.incarnation, snapshot),
            )
            for thread in snapshot.threads.values()
        )
        candidates = tuple(reading for reading in readings if reading.thread.name != commander)
        eligible = set(held)
        eligible.update(
            reading.thread.incarnation
            for reading in candidates
            if reading.thread.parent.named(commander)
        )
        eligible.update(
            reading.thread.incarnation
            for reading in candidates
            if reading.thread.role.executor(reading.thread) and reading.presence.receiving
        )
        return tuple(reading for reading in readings if reading.thread.incarnation in eligible)

    @classmethod
    def current(cls, comms: Comms, commander: str, pulls: ProgressRead) -> tuple[UnitState, ...]:
        states = tuple(cls.read(source) for source in cls.observations(comms, commander, pulls))
        return tuple(
            state for member in cls.worst_first() for state in states if isinstance(state, member)
        )

    @classmethod
    @abstractmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]: ...

    @property
    def owed(self) -> tuple[Message, ...]:
        return self.source.due_questions

    def readable_units(self) -> tuple[ThreadIncarnation, ...]:
        return (self.unit,)

    def attention(self) -> bool:
        return False

    def merged_orders(self, comms: Comms, pulls: ProgressRead) -> tuple[Todo, ...]:
        return tuple(
            closed
            for problem in self.orders
            for todo in (comms.goals.todos.get(problem),)
            for closed in todo.order.closed_by_merge(todo, comms.registry, pulls)
        )

    @property
    def default_action(self) -> str:
        return self.action

    def detail(self) -> str:
        return f"orders {', '.join(self.orders)}"

    def line(self) -> str:
        return f"{type(self).__name__} {self.unit.name}: {self.detail()} -> {self.default_action}"


class ConditionalState(UnitState, declared_name=None):
    """Abstract card construction shared by siblings, not a separate unit state."""

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        if cls.matches(source):
            yield cls(source.thread.incarnation, source.orders, source.presence, source)

    @classmethod
    @abstractmethod
    def matches(cls, source: UnitObservation) -> bool: ...


class PendingWorkState(ConditionalState, declared_name=None):
    """These sibling states require pending work; declare that condition once."""

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        if source.pending:
            yield from super().derive(source)


@dataclass(frozen=True)
class Unreadable(NeedsCommanderAttention, UnitState):
    reason: str
    action = "repair the unreadable source; do not infer that the unit is idle"

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        # Validate the checkout reads before a higher-level label can hide a failure.
        source.require_read()
        return iter(())

    def readable_units(self) -> tuple[ThreadIncarnation, ...]:
        return ()

    def merged_orders(self, comms: Comms, pulls: ProgressRead) -> tuple[Todo, ...]:
        # The card reports the failure; a merge read must not retry that source.
        return ()

    def detail(self) -> str:
        return f"{super().detail()}; {self.reason}"


@dataclass(frozen=True)
class Blocked(NeedsCommanderAttention, UnitState):
    goal: GoalSnapshot
    action = "follow the blocked goal's declared response"

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        if not source.pending:
            return
        if source.goal.execution.unit_blocks():
            yield cls(
                source.thread.incarnation,
                tuple(todo.id for todo in source.pending),
                source.presence,
                source,
                source.goal,
            )

    def line(self) -> str:
        return "\n".join(
            block.unit_block_line(self.unit.name, self.orders)
            for block in self.goal.execution.unit_blocks()
        )


@dataclass(frozen=True)
class Detached(NeedsCommanderAttention, UnitState):
    unread: int = field(compare=False)
    action = "wake it or reassign the order"

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        if source.pending and not source.presence.receiving:
            yield cls(
                source.thread.incarnation,
                source.orders,
                source.presence,
                source,
                source.comms.bus.pending_count(source.thread.name),
            )

    def detail(self) -> str:
        return f"{self.unread} unread; {super().detail()}"


@dataclass(frozen=True)
class AwaitingCommander(NeedsCommanderAttention, UnitState):
    seconds: ClassVar[int] = 5 * 60
    action = "answer the original message"

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        if source.due_questions:
            yield cls(source.thread.incarnation, source.orders, source.presence, source)

    def detail(self) -> str:
        return ", ".join(
            f"message {message.reference.seq}:{message.message_id}" for message in self.owed
        )


class Stuck(NeedsCommanderAttention, PendingWorkState, UnitState):
    seconds: ClassVar[int] = 20 * 60
    action = "inspect its last turn and restart only known-safe work"

    @classmethod
    def matches(cls, source: UnitObservation) -> bool:
        calls = source.calls
        latest = max(
            (event.timestamp for turn in source.turns for event in turn.events),
            default=source.since,
        )
        return bool(
            not calls
            and any(
                todo.order.checked().first_call_deadline.before(source.now)
                for todo in source.pending
            )
            or calls
            and latest < source.now - cls.seconds
        )


class Spinning(NeedsCommanderAttention, PendingWorkState, UnitState):
    action = "read the failed approach and change it before another rerun"

    @classmethod
    def matches(cls, source: UnitObservation) -> bool:
        sends = sorted(
            at
            for todo in source.pending
            for verdict in todo.verdicts
            for at in verdict.send_back_times()
        )
        calls = source.calls
        repeats = len(calls) >= 2 and calls[-1].digest == calls[-2].digest
        if len(sends) < 2 and not repeats:
            return False
        commit_at = max(
            (commit.at for origin in source.sources for commit in origin.commits()),
            default=source.since,
        )
        return (
            len(sends) >= 2
            and sends[-2] > commit_at
            or repeats
            and source.turns[-1].start.since(commit_at)
        )


class StalledOnNothing(NeedsCommanderAttention, PendingWorkState, UnitState):
    action = "resume its work or give it the missing next step"

    @classmethod
    def matches(cls, source: UnitObservation) -> bool:
        return bool(
            source.goal.execution.runnable()
            and source.turns
            and not source.comms.bus.pending_count(source.thread.name)
        )


class DeliveredUnclosed(NeedsCommanderAttention, ConditionalState, UnitState):
    action = "install the merge or close with the working application evidence"

    @classmethod
    def matches(cls, source: UnitObservation) -> bool:
        progress: dict[str, OrderProgress] = {
            origin.todo.id: next(MergedProgress.found(origin), NoCommitProgress())
            for origin in source.sources
        }
        return any(
            todo.order.accepted or todo.order.waiting_on_install(todo, progress)
            for todo in source.todos
        )


class Moving(ConditionalState, UnitState):
    action = "let it continue"

    @classmethod
    def matches(cls, source: UnitObservation) -> bool:
        return bool(source.todos)


class Idle(ConditionalState, UnitState):
    action = "give it the next useful order"

    @classmethod
    def matches(cls, source: UnitObservation) -> bool:
        return not source.todos
