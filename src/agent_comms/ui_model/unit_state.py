"""One derived card per unit, ordered by the family's declaration order.

States are siblings, not increasingly urgent subtypes of one another. The
StandingRule registry supplies both membership and urgency; no second roster
or independently writable unit status is retained.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from subprocess import TimeoutExpired
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.comms import Comms
from agent_comms.declared_family import DeclaredFamily
from agent_comms.disclosure_text import AccentTone, SuccessTone, ViewHeading, WarningTone
from agent_comms.field_codec import (
    MalformedRecord,
    ToolCallId,
)
from agent_comms.goal_presentation import GoalSnapshot
from agent_comms.heartbeat_subjects import HeartbeatSubject, UnitSubject
from agent_comms.messages import Message
from agent_comms.native_failures import NativePiUnavailable
from agent_comms.repo import GitCommandError
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import FamilyScan, UndatedStandingRule
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.thread_status import ThreadStatus
from agent_comms.threads import Thread
from agent_comms.todos import (
    MergedProgress,
    NoCommitProgress,
    OrderProgress,
    ProgressSource,
    Todo,
)
from agent_comms.transcript_events import OrderCall, TurnDigest
from agent_comms.turn_digests import LastTurns, TurnDigestRead
from agent_comms.work_branches import WorkBranchError

if TYPE_CHECKING:
    from agent_comms.heartbeat_frame import HeartbeatFrame


class UnitClock(DeclaredFamily, affix="Clock"):
    """When a holder last acted and last committed on its pending orders."""

    @abstractmethod
    def line(self, now: float) -> str: ...


@dataclass(frozen=True)
class PendingClock(UnitClock):
    last_call_at: float
    last_commit_at: float

    def line(self, now: float) -> str:
        return (
            f"last call {round((now - self.last_call_at) / 60)} min ago, "
            f"last commit {round((now - self.last_commit_at) / 60)} min ago"
        )


@dataclass(frozen=True)
class NoPendingClock(UnitClock):
    def line(self, now: float) -> str:
        return "no pending order"


@dataclass(frozen=True)
class UnreadableClock(UnitClock):
    """Its checkout cannot be read, so neither time is known."""

    def line(self, now: float) -> str:
        return "times unreadable"


@dataclass(frozen=True)
class UnitObservation:
    """Original sources shared by candidate states within this one read."""

    comms: Comms
    thread: Thread
    todos: tuple[Todo, ...]
    frame: HeartbeatFrame
    questions: tuple[Message, ...]
    now: float
    presence: ThreadStatus
    working: bool

    @cached_property
    def review_timers(self) -> tuple[tuple[Todo, RecordedEventTime], ...]:
        return tuple((todo, at) for todo, at in self.frame.review_timers
                     if todo.held().owner_identity == self.thread.incarnation)

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
    def between_turns(self) -> bool:
        """Core lets the goal run and no bridge reports a live turn."""
        return self.goal.execution.runnable() and not self.working

    @cached_property
    def pending(self) -> tuple[Todo, ...]:
        return tuple(todo for todo in self.todos if todo.pending)

    @cached_property
    def turns(self) -> tuple[TurnDigest, ...]:
        return TurnDigestRead(self.comms.root, self.thread, LastTurns(2)).read().turns

    @cached_property
    def calls(self) -> tuple[OrderCall, ...]:
        calls: dict[ToolCallId, OrderCall] = {}
        for turn in reversed(self.turns):
            for event in turn.events:
                event.record_call(calls, self.since)
        return tuple(calls.values())

    @cached_property
    def since(self) -> float:
        return min(todo.order.checked().at.recorded_at("A held order") for todo in self.pending)

    @cached_property
    def last_call_at(self) -> float:
        """The newest recorded event of its last turns, or the order's start without one."""
        return max(
            (event.timestamp for turn in self.turns for event in turn.events), default=self.since
        )

    @cached_property
    def last_commit_at(self) -> float:
        return max(
            (commit.at for origin in self.sources for commit in origin.commits()),
            default=self.since,
        )

    @cached_property
    def clock(self) -> UnitClock:
        if self.pending:
            return PendingClock(self.last_call_at, self.last_commit_at)
        return NoPendingClock()

    @cached_property
    def sources(self) -> tuple[ProgressSource, ...]:
        return tuple(
            ProgressSource.of(todo, self.comms.registry, self.frame.pulls) for todo in self.todos
        )

    def require_read(self) -> None:
        for progress in self.pulls.progress(self.todos, self.comms.registry).values():
            progress.require_read()
        for todo in self.todos:
            reader = self.pulls.checkout(todo.order.checked().check.directory).branches
            reader.require_prepared()
            for pull in self.pulls.open_for(todo):
                pull.require_comparison(reader)
        if self.todos:
            for path in self.thread.home_checkouts:
                self.frame.pulls.branch_difference(path)
            for path in self.comms.registry.storage_rule.checkout_paths(self.thread):
                self.frame.pulls.checkout(path).require_read()
        for source in self.sources:
            source.checkout.require_read()

    @property
    def orders(self) -> tuple[str, ...]:
        return tuple(todo.id for todo in self.todos)

    def merges(self) -> dict[str, OrderProgress]:
        """Each held order's merge as its progress reads it; unmerged orders read no commit."""
        return {origin.todo.id: next(MergedProgress.found(origin), NoCommitProgress())
                for origin in self.sources}


class ExpandedHudGroup:
    """Expanded sections consume their tables; collapsed sections do not prepare them."""

    @classmethod
    def table_lines(cls, tables: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
        return tuple(line for table in tables for line in table)


class HudGroup(ViewHeading, affix="Group"):
    """The declared order and headings shared by heartbeat and native table views."""

    @classmethod
    def table_lines(cls, tables: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
        return ()

    @classmethod
    def needs_hand(cls) -> bool:
        return False

class UrgentGroup(ExpandedHudGroup, HudGroup):
    tone = WarningTone

    @classmethod
    def needs_hand(cls) -> bool:
        return True


class InProgressGroup(ExpandedHudGroup, HudGroup):
    tone = SuccessTone


class ReadyToTestGroup(ExpandedHudGroup, HudGroup):
    tone = AccentTone


class IdleGroup(HudGroup):
    pass


class ReadyForApplication:
    """Delivered and installed states share their declared presentation capability."""

    hud_group: ClassVar[type[HudGroup]] = ReadyToTestGroup


class UrgentPresentation:
    """A row can be urgent even when another rule already owns its request."""

    hud_group: ClassVar[type[HudGroup]] = UrgentGroup


class NeedsCommanderAttention(UrgentPresentation):
    """Urgent siblings share their ping behavior without claiming each other's state."""

    @classmethod
    def attention(cls) -> bool:
        return True


@dataclass(frozen=True)
class UnitState(UndatedStandingRule, FamilyScan, declared_name=None):
    unit: ThreadIncarnation
    orders: tuple[str, ...]
    presence: ThreadStatus
    source: UnitObservation = field(compare=False, repr=False)
    action: ClassVar[str]
    hud_group: ClassVar[type[HudGroup]] = IdleGroup
    # What the owner would ask on reading this card; the action answers it.
    question: ClassVar[str]

    @classmethod
    def worst_first(cls) -> tuple[type[UnitState], ...]:
        return UnitState.members_with(UnitState)

    @classmethod
    def scan_observed(
        cls, comms: Comms, commander: str, observed: Sequence[UnitState], frame: HeartbeatFrame
    ) -> tuple[Self, ...]:
        # States are already read; ordinary scans consume them rather than emit them again.
        return ()

    @classmethod
    def read(cls, source: UnitObservation) -> UnitState:
        try:
            source.require_read()
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
        cls, comms: Comms, commander: str, frame: HeartbeatFrame
    ) -> tuple[UnitObservation, ...]:
        snapshot, now = comms.registry.snapshot(), time.time()
        held = frame.holders
        readings = tuple(
            UnitObservation(
                comms,
                thread,
                tuple(held.get(thread.incarnation, ())),
                frame,
                tuple(
                    message
                    for rule in frame.answers
                    for message in rule.questions_for(thread.incarnation)
                ),
                now,
                snapshot.status(thread.name),
                comms.agents.is_working(thread.incarnation, snapshot),
            )
            for thread in snapshot.threads.values()
        )
        candidates = tuple(
            reading
            for reading in readings
            if reading.thread.name != commander
            for _ in reading.thread.execution.commanded_units(reading.thread)
        )
        eligible = set(held)
        eligible.update(
            reading.thread.incarnation
            for reading in candidates
            if reading.thread.role.executor(reading.thread) and reading.presence.receiving
        )
        return tuple(reading for reading in readings if reading.thread.incarnation in eligible)

    @classmethod
    def current(cls, comms: Comms, commander: str, frame: HeartbeatFrame) -> tuple[UnitState, ...]:
        states = tuple(cls.read(source) for source in cls.observations(comms, commander, frame))
        return tuple(sorted(states, key=lambda state: state.declared_position()))

    @classmethod
    @abstractmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]: ...

    @property
    def owed(self) -> tuple[Message, ...]:
        return self.source.due_questions

    def readable_units(self) -> tuple[ThreadIncarnation, ...]:
        return (self.unit,)

    def clock(self) -> UnitClock:
        return self.source.clock

    @classmethod
    def attention(cls) -> bool:
        return False

    @property
    def default_action(self) -> str:
        return self.action

    def subjects(self) -> tuple[HeartbeatSubject, ...]:
        return (UnitSubject(self.unit),)

    def detail(self) -> str:
        return " ".join(self.orders)

    def line(self) -> str:
        return f"{self.unit.name} {self.detail()}. {self.question} -> {self.default_action}"


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
    question = "its source doesn't even read, so how would we know it's idle?"
    action = "repair the unreadable source; do not infer that the unit is idle"

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        # UnitState.read validates once before selecting any card.
        return iter(())

    def readable_units(self) -> tuple[ThreadIncarnation, ...]:
        return ()

    def clock(self) -> UnitClock:
        return UnreadableClock()

    def detail(self) -> str:
        return f"{super().detail()}; {self.reason}"


@dataclass(frozen=True)
class Blocked(NeedsCommanderAttention, UnitState):
    goal: GoalSnapshot
    question = "its goal is blocked. what does it need?"
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
    """A pending order whose holder's presence takes no delivery: an order sent now is not
    read now. The registered status owns that fact; the inbox's read marker is the owner's,
    marks what he opened, and says nothing about the unit."""

    question = "is anyone home, or are we yelling into an inbox?"
    action = "wake it or reassign the order"

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        if source.pending and not source.presence.receiving:
            yield cls(source.thread.incarnation, source.orders, source.presence, source)

    def detail(self) -> str:
        return f"{self.presence.declared_name}, takes no delivery; {super().detail()}"


@dataclass(frozen=True)
class AwaitingCommander(NeedsCommanderAttention, UnitState):
    seconds: ClassVar[int] = 5 * 60
    question = "it asked you. why is it still waiting?"
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
    question = "is it doing work, or letting work accumulate?"
    action = "inspect its last turn and restart only known-safe work"

    @classmethod
    def matches(cls, source: UnitObservation) -> bool:
        calls = source.calls
        return bool(
            not calls
            and any(
                todo.order.checked().first_call_deadline.before(source.now)
                for todo in source.pending
            )
            or calls
            and source.last_call_at < source.now - cls.seconds
        )


class Spinning(NeedsCommanderAttention, PendingWorkState, UnitState):
    question = "same failure again. why would the next rerun be different?"
    action = "read the failed approach and change it before another rerun"

    @classmethod
    def matches(cls, source: UnitObservation) -> bool:
        sends = sorted(
            at
            for todo in source.pending
            for at in todo.history.facts_for(todo.id).send_backs[:2]
        )
        calls = source.calls
        # The same call failing twice is a repeated failure; a repeated success or a call
        # still running is ordinary work, such as re-arming a watch with its one command.
        repeats = len(calls) >= 2 and calls[-1].repeats_failure(calls[-2])
        if len(sends) < 2 and not repeats:
            return False
        commit_at = source.last_commit_at
        return (
            len(sends) >= 2
            and sends[-2] > commit_at
            or repeats
            and source.turns[-1].start.since(commit_at)
        )


class StalledOnNothing(NeedsCommanderAttention, PendingWorkState, UnitState):
    question = "its turn ended with nothing queued. stalled on what, exactly?"
    action = "resume its work or give it the missing next step"

    @classmethod
    def matches(cls, source: UnitObservation) -> bool:
        return bool(
            source.between_turns
            and source.turns
            and not source.comms.bus.pending_count(source.thread.name)
        )


class ReviewDue(UrgentPresentation, ConditionalState, UnitState):
    """The frame's review rounds require the commander even while the unit keeps working."""

    question = "its review timer is up. what did it actually do?"
    action = "read its diff and judge the recorded work"

    @classmethod
    def matches(cls, source: UnitObservation) -> bool:
        return bool(source.review_timers)


class DeliveredUnclosed(ReadyForApplication, NeedsCommanderAttention, ConditionalState, UnitState):
    question = "delivered and still open. why isn't it closed?"
    action = "install the merge or close with the working application evidence"

    @classmethod
    def matches(cls, source: UnitObservation) -> bool:
        progress = source.merges()
        return any(
            todo.order.accepted
            or (todo.order.waiting_on_install(todo, progress) and not todo.order.awaits_holder_run())
            for todo in source.todos
        )


class AwaitingApplicationRun(ReadyForApplication, NeedsCommanderAttention, UnitState):
    """Installed merged orders wait on their holder's application run, so no commander verb
    disposes of the line and it locks none. The line names only those orders; while the unit
    still writes source for another, that order's state is the unit's."""

    question = "installed. did its holder actually run it yet?"
    action = "its holder runs the application proof on the installed build; close it on its evidence"
    commander_disposes = False

    @classmethod
    def derive(cls, source: UnitObservation) -> Iterator[Self]:
        merges = source.merges()
        if any(todo.order.writes_source(todo, merges) for todo in source.todos):
            return
        awaiting = tuple(todo.id for todo in source.todos if todo.order.awaits_holder_run())
        if awaiting:
            yield cls(source.thread.incarnation, awaiting, source.presence, source)


class Moving(ConditionalState, UnitState):
    hud_group = InProgressGroup
    question = "it's moving. toward the order, or somewhere else?"
    action = "let it continue"

    @classmethod
    def matches(cls, source: UnitObservation) -> bool:
        return bool(source.todos)


class Idle(ConditionalState, UnitState):
    question = "nothing to do. why is it idle?"
    action = "give it the next useful order"

    @classmethod
    def matches(cls, source: UnitObservation) -> bool:
        return not source.todos
