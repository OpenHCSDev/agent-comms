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
from typing import ClassVar, Self

from agent_comms.comms import Comms
from agent_comms.execution.native_failures import NativePiUnavailable
from agent_comms.heartbeat_subjects import HeartbeatSubject, TeammateSubject
from agent_comms.messaging.messages import Message
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    DisclosedRecord,
    MalformedRecord,
    ToolCallId,
)
from agent_comms.planning.goal_presentation import GoalSnapshot
from agent_comms.planning.orders import (
    MergedProgress,
    NoCommitProgress,
    OrderProgress,
    ProgressSource,
    Todo,
)
from agent_comms.projects.git_errors import GitCommandError
from agent_comms.projects.work_branches import WorkBranchError
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.thread_status import ThreadStatus
from agent_comms.registry.threads import Thread
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.team.standing_rules import (
    FamilyScan,
    HeartbeatFrame,
    ObservedTeammate,
    TeammateEvidence,
    UndatedStandingRule,
)
from agent_comms.transcript.transcript_events import OrderCall, TurnDigest
from agent_comms.ui_model.hud_groups import (
    ExpandedHudGroup,
    HudGroup,
    IdleGroup,
    InProgressGroup,
    ParkedGroup,
    ReadyToTestGroup,
    UrgentGroup,
)


class TeammateClock(DisclosedRecord, DeclaredFamily, affix="Clock"):
    """When a holder last acted and last committed on its pending orders."""

    @abstractmethod
    def line(self, now: float) -> str: ...


@dataclass(frozen=True)
class PendingClock(TeammateClock):
    last_call_at: float
    last_commit_at: float

    def line(self, now: float) -> str:
        return (
            f"last call {round((now - self.last_call_at) / 60)} min ago, "
            f"last commit {round((now - self.last_commit_at) / 60)} min ago"
        )


@dataclass(frozen=True)
class NoPendingClock(TeammateClock):
    def line(self, now: float) -> str:
        return "no pending order"


@dataclass(frozen=True)
class UnreadableClock(TeammateClock):
    """Its checkout cannot be read, so neither time is known."""

    def line(self, now: float) -> str:
        return "times unreadable"


@dataclass(frozen=True)
class TeammateObservation(TeammateEvidence):
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
            if message.timestamp <= self.now - AwaitingTeam.seconds
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
        return self.frame.recent_turns(self.comms, self.thread)

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
    def clock(self) -> TeammateClock:
        if self.pending:
            return PendingClock(self.last_call_at, self.last_commit_at)
        return NoPendingClock()

    @cached_property
    def sources(self) -> tuple[ProgressSource, ...]:
        return tuple(
            ProgressSource.of(todo, self.comms.registry, self.frame.pulls) for todo in self.todos
        )

    def require_read(self) -> None:
        for progress in self.frame.pulls.progress(self.todos, self.comms.registry).values():
            progress.require_read()
        for todo in self.todos:
            reader = self.frame.pulls.checkout(todo.order.checked().check.directory).branches
            for pull in self.frame.pulls.open_for(todo):
                pull.require_comparison(reader)
        if self.todos:
            for path in self.comms.registry.storage_rule.checkout_paths(self.thread, self.frame.pulls):
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


class ReadyForApplication:
    """Delivered and installed states share their declared presentation capability."""

    hud_group: ClassVar[type[HudGroup]] = ReadyToTestGroup


class UrgentPresentation:
    """A row can be urgent even when another rule already owns its request."""

    hud_group: ClassVar[type[HudGroup]] = UrgentGroup


class NeedsTeamAttention(UrgentPresentation):
    """Urgent siblings share their ping behavior without claiming each other's state."""

    @classmethod
    def attention(cls) -> bool:
        return True


@dataclass(frozen=True)
class TeammateState(UndatedStandingRule, FamilyScan, ObservedTeammate, declared_name=None):
    unit: ThreadIncarnation
    orders: tuple[str, ...]
    presence: ThreadStatus
    source: TeammateObservation = field(compare=False, repr=False)
    action: ClassVar[str]
    hud_group: ClassVar[type[HudGroup]] = IdleGroup
    # What the owner would ask on reading this card; the action answers it.
    question: ClassVar[str]

    @classmethod
    def worst_first(cls) -> tuple[type[TeammateState], ...]:
        return TeammateState.members_with(TeammateState)

    @classmethod
    def scan_observed(
        cls, comms: Comms, observed: Sequence[TeammateState], frame: HeartbeatFrame
    ) -> tuple[Self, ...]:
        # States are already read; ordinary scans consume them rather than emit them again.
        return ()

    @classmethod
    def read(cls, source: TeammateObservation) -> TeammateState:
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
        cls, comms: Comms, frame: HeartbeatFrame
    ) -> tuple[TeammateObservation, ...]:
        snapshot, now = comms.registry.snapshot(), time.time()
        held = frame.holders
        readings = tuple(
            TeammateObservation(
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
            for _ in reading.thread.independent_threads()
        )
        eligible = set(held)
        eligible.update(
            reading.thread.incarnation
            for reading in candidates
            if reading.thread.role.executor(reading.thread) and reading.presence.is_receiving()
        )
        return tuple(reading for reading in readings if reading.thread.incarnation in eligible)

    @classmethod
    def current(cls, comms: Comms, frame: HeartbeatFrame) -> tuple[TeammateState, ...]:
        states = tuple(cls.read(source) for source in cls.observations(comms, frame))
        return tuple(sorted(states, key=lambda state: state.declared_position()))

    @classmethod
    @abstractmethod
    def derive(cls, source: TeammateObservation) -> Iterator[Self]: ...

    @property
    def owed(self) -> tuple[Message, ...]:
        return self.source.due_questions

    def readable_units(self) -> tuple[ThreadIncarnation, ...]:
        return (self.unit,)

    def clock(self) -> TeammateClock:
        return self.source.clock

    @classmethod
    def attention(cls) -> bool:
        return False

    @property
    def default_action(self) -> str:
        return self.action

    def subjects(self) -> tuple[HeartbeatSubject, ...]:
        return (TeammateSubject(self.unit),)

    def detail(self) -> str:
        return " ".join(self.orders)

    def line(self) -> str:
        return f"{self.unit.name} {self.detail()}. {self.question} -> {self.default_action}"


class ConditionalState(TeammateState, declared_name=None):
    """Abstract card construction shared by siblings, not a separate unit state."""

    @classmethod
    def derive(cls, source: TeammateObservation) -> Iterator[Self]:
        if cls.matches(source):
            yield cls(source.thread.incarnation, source.orders, source.presence, source)

    @classmethod
    @abstractmethod
    def matches(cls, source: TeammateObservation) -> bool: ...


class PendingWorkState(ConditionalState, declared_name=None):
    """These sibling states require pending work; declare that condition once."""

    @classmethod
    def derive(cls, source: TeammateObservation) -> Iterator[Self]:
        if source.pending:
            yield from super().derive(source)


@dataclass(frozen=True)
class Unreadable(NeedsTeamAttention, TeammateState):
    reason: str
    question = "its source doesn't even read, so how would we know it's idle?"
    action = "repair the unreadable source; do not infer that the teammate is idle"

    @classmethod
    def derive(cls, source: TeammateObservation) -> Iterator[Self]:
        # TeammateState.read validates once before selecting any card.
        return iter(())

    def readable_units(self) -> tuple[ThreadIncarnation, ...]:
        return ()

    def clock(self) -> TeammateClock:
        return UnreadableClock()

    def detail(self) -> str:
        return f"{super().detail()}; {self.reason}"


@dataclass(frozen=True)
class Detached(NeedsTeamAttention, TeammateState):
    """A stopped holder: a name still on open orders that no longer takes delivery.

    This card can only ever be a holder. observations() keeps a unit that is not
    receiving exactly when it is in `held` (eligible = set(held), then receiving
    executors), so a stopped unit with nothing assigned is not observed at all. It
    nonetheless described itself as parked, sat below the working groups in ParkedGroup,
    raised no line, and offered "leave it parked" as the first half of its action.

    "Held" and "its holder still answers" were one card's two facts joined by whoever
    read it, and every reader took the cheap half: a name beside an order reads as taken,
    so nobody checks whether that name still takes delivery. On 2026-11-10 one stopped
    session held four orders while five teammates sat idle with none, and the board
    raised no line for any of it.

    A name does not hold an order; a unit that reads its inbox does. So this asks for a
    hand and its action names the next move instead of offering to do nothing.
    """

    hud_group = UrgentGroup
    question = "it's stopped and still holds orders. who takes them?"
    action = "take its orders, or release them with order fire; a stopped name owns nothing"

    @classmethod
    def derive(cls, source: TeammateObservation) -> Iterator[Self]:
        if not source.presence.is_receiving():
            yield cls(source.thread.incarnation, source.orders, source.presence, source)

    def detail(self) -> str:
        return (f"{self.presence.declared_name}, takes no delivery, still holds "
                f"{len(self.orders)}: {', '.join(self.orders)}; {super().detail()}")


@dataclass(frozen=True)
class Blocked(NeedsTeamAttention, TeammateState):
    goal: GoalSnapshot
    question = "its goal is blocked. what does it need?"
    action = "follow the blocked goal's declared response"

    @classmethod
    def derive(cls, source: TeammateObservation) -> Iterator[Self]:
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
class AwaitingTeam(NeedsTeamAttention, TeammateState, declared_name="awaiting_team"):
    seconds: ClassVar[int] = 5 * 60
    question = "it asked you. why is it still waiting?"
    action = "answer the original message"

    @classmethod
    def derive(cls, source: TeammateObservation) -> Iterator[Self]:
        if source.due_questions:
            yield cls(source.thread.incarnation, source.orders, source.presence, source)

    def detail(self) -> str:
        return ", ".join(
            f"message {message.reference.seq}:{message.message_id}" for message in self.owed
        )


class Stuck(NeedsTeamAttention, PendingWorkState, TeammateState):
    seconds: ClassVar[int] = 20 * 60
    question = "is it doing work, or letting work accumulate?"
    action = "inspect its last turn and restart only known-safe work"

    @classmethod
    def matches(cls, source: TeammateObservation) -> bool:
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


class Spinning(NeedsTeamAttention, PendingWorkState, TeammateState):
    question = "same failure again. why would the next rerun be different?"
    action = "read the failed approach and change it before another rerun"

    @classmethod
    def matches(cls, source: TeammateObservation) -> bool:
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


class StalledOnNothing(NeedsTeamAttention, PendingWorkState, TeammateState):
    question = "its turn ended with nothing queued. stalled on what, exactly?"
    action = "resume its work or give it the missing next step"

    @classmethod
    def matches(cls, source: TeammateObservation) -> bool:
        return bool(
            source.between_turns
            and source.turns
            and not source.comms.bus.pending_count(source.thread.name)
        )


class ReviewDue(UrgentPresentation, ConditionalState, TeammateState):
    """The frame's review rounds require the teammate even while the unit keeps working."""

    question = "its review timer is up. what did it actually do?"
    action = "read its diff and judge the recorded work"

    @classmethod
    def matches(cls, source: TeammateObservation) -> bool:
        return bool(source.review_timers)


class DeliveredUnclosed(ReadyForApplication, NeedsTeamAttention, ConditionalState, TeammateState):
    question = "delivered and still open. why isn't it closed?"
    action = "install the merge or close with the working application evidence"

    @classmethod
    def matches(cls, source: TeammateObservation) -> bool:
        progress = source.merges()
        return any(
            todo.order.accepted
            or (todo.order.waiting_on_install(todo, progress) and not todo.order.awaits_holder_run())
            for todo in source.todos
        )


class AwaitingApplicationRun(ReadyForApplication, NeedsTeamAttention, TeammateState):
    """Installed merged orders wait on their holder's application run, so no teammate verb
    disposes of the line and it locks none. The line names only those orders; while the unit
    still writes source for another, that order's state is the unit's."""

    question = "installed. did its holder actually run it yet?"
    action = "its holder runs the application proof on the installed build; close it on its evidence"
    team_disposes = False

    @classmethod
    def derive(cls, source: TeammateObservation) -> Iterator[Self]:
        merges = source.merges()
        if any(todo.order.writes_source(todo, merges) for todo in source.todos):
            return
        awaiting = tuple(todo.id for todo in source.todos if todo.order.awaits_holder_run())
        if awaiting:
            yield cls(source.thread.incarnation, awaiting, source.presence, source)


class Moving(ConditionalState, TeammateState):
    hud_group = InProgressGroup
    question = "it's moving. toward the order, or somewhere else?"
    action = "let it continue"

    @classmethod
    def matches(cls, source: TeammateObservation) -> bool:
        return bool(source.todos)


class Idle(ConditionalState, TeammateState):
    question = "nothing to do. why is it idle?"
    action = "give it the next useful order"

    @classmethod
    def matches(cls, source: TeammateObservation) -> bool:
        return not source.todos
