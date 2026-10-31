"""When the fleet's signals degrade, the heartbeat stops the commander with its indictment.

A degraded signal is a fact Core already reads that no heartbeat line states: a load past the
ceiling. An owed answer or a stalled plan phase is a heartbeat line already, on the board the
commander's tool calls print, so it never stops the commander a second time here. The
vibe-check sender puts the indictment in front of the commander, and every verb that answers
nothing is refused until the commander answers that message (the OwedAnswer gate)."""

from __future__ import annotations

from agent_comms.nominal.field_codec import WireExclude

import time
from collections.abc import Mapping, Sequence
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Self

from agent_comms.bus import BusReader
from agent_comms.commander.package import CommanderPackage
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import projected
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.goal_actions import (
    ChokeholdPausedGoalAction,
    CompliedGoalAction,
    OwnerInvocable,
)
from agent_comms.messages import Message
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.task_attachments.owner_notices import ChokeholdNotice, EscalationNotice, VibeCheckNotice
from agent_comms.standing_rules import (
    BlocksRelease,
    Degradation,
    Sign,
    IndependentStandingRule,
    OwedAnswer,
    ReviewTimerUp,
    StandingRule,
    FamilyScan,
    UndatedStandingRule,
)
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.held_orders import HeldOrders
from agent_comms.todos import OwnerRequest, Todo
from agent_comms.tool_result import ToolResult
from agent_comms.ui_model.unit_state import UnitState


class Escalation:
    """An answer owed to an escalation itself. Ignoring one is the chokehold's signal."""

    message: Message
    notice: ClassVar[type[EscalationNotice]]

    def clearing_action(self) -> str:
        return (f"Answer the {self.notice.title} burst by its protocol, naming it with "
                f"--answers {self.message.message_id}.")

    def answered_by(self, named: tuple[Message, ...]) -> bool:
        """An escalation arrives as a burst: an answer naming any line of it covers every line
        of that burst sent before the answer, which unanswered reads once the answer is sent.
        The gate runs just before the answer is sent, so it reads the answer the same way."""
        return any(message.task == self.message.task for message in named)


@dataclass(frozen=True)
class OverloadDegradation(Degradation, BlocksRelease, ToolResult):
    """More owed orders, expired timers and unjudged edits than one commander keeps up with.
    Every edit a verdict can still clear is a point, so one unit running unwatched trips it
    alone; edits under a merged order are nobody's review and count nothing. Forks relieve it
    by judging and ordering, which lowers the debt; load moves every beat, so it stops the
    commander once per round while it stays over."""

    ceiling: ClassVar[float] = 100.0
    round_seconds: ClassVar[int] = 30 * 60

    owed: int
    timers_up: int
    unjudged: int

    @classmethod
    def from_frame(cls, comms: Comms, commander: str, frame: HeartbeatFrame) -> Self:
        return cls.from_orders(
            frame.todos,
            len(tuple(ReviewTimerUp.conditions(comms, frame))),
            sum(frame.edit_counts(comms).values()),
        )

    @classmethod
    def from_orders(cls, orders: Sequence[Todo], timers_up: int, unjudged: int) -> Self:
        """One load projection shared by the heartbeat's HUD and standing rules."""
        return cls(sum(todo.pending for todo in orders), timers_up, unjudged)

    @projected(view=WireExclude)
    def total(self) -> float:
        return self.owed + 2 * self.timers_up + self.unjudged

    @projected(view=WireExclude)
    def overloaded(self) -> bool:
        return self.total >= self.ceiling

    def __str__(self) -> str:
        return (
            f"load {self.total:.0f} ({self.owed} open orders, "
            f"{self.timers_up} review timers up, {self.unjudged} unjudged edits)"
        )

    @classmethod
    def found(cls, comms: Comms, commander: str, frame: HeartbeatFrame) -> tuple[Sign, ...]:
        load = cls.from_frame(comms, commander, frame)
        if not load.overloaded:
            return ()
        return (Sign(cls, (int(time.time() // cls.round_seconds),),
                     f"{load}, over {cls.ceiling:.0f}"),)


@dataclass(frozen=True)
class VibeCheck(UndatedStandingRule):
    """Each degraded sign breaks it once: the heartbeat enforces a rule when it newly breaks, so a
    sign that persists stops the commander once, and a new message, a newly stalled phase or a
    doubled stall stops it again. Pings are decisions, each once."""

    sign: Sign

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return tuple(
            cls(sign)
            for member in Degradation.members_with(Degradation)
            for sign in member.found(comms, commander, frame)
        )

    def enforce(self, comms: Comms, commander: str) -> None:
        indictment = self.indictment()
        VibeCheckNotice.burst_once(comms, commander, indictment, indictment[0])

    def indictment(self) -> tuple[str, ...]:
        return (
            f"VIBE CHECK ({self.sign.mark}). This is about you, right now, not the units.",
            self.sign.line,
            f"Stop. Reread {CommanderPackage.indictment()} in full. You are doing what it"
            " describes, this hour.",
            "Answer this burst once, naming any one of its messages with --answers: which failure"
            " in the indictment is you right now, why, and the one structural change you make"
            " this turn. Every verb that answers nothing is refused until you do. Ignore this and"
            " the fleet stops.",
        )

    def line(self) -> str:
        return (f"vibe check: {self.sign.signal.declared_name}. you seeing this?"
                f" -> answer the {VibeCheckNotice.title} messages now")


@dataclass(frozen=True)
class VibeCheckUnanswered(Escalation, OwedAnswer, UndatedStandingRule):
    """A vibe check is owed an answer like a directive: the gate refuses until it gets one."""

    notice = VibeCheckNotice

    seconds: ClassVar[int] = 5 * 60

    @classmethod
    def read_open(cls, source: BusReader, snapshot: RegistrySnapshot,
                  held: Mapping[ThreadIncarnation, Sequence[Todo]], commander: str,
                  requests: Sequence[OwnerRequest]) -> tuple[Self, ...]:
        return tuple(cls(message) for message in
                     VibeCheckNotice.unanswered(source, snapshot, commander, time.time()))

    def due(self, now: float) -> bool:
        return self.message.timestamp <= now - self.seconds

    def line(self) -> str:
        return (f"vibe check {self.cited} from {RecordedEventTime(self.message.timestamp).stamp()}."
                " still ignoring it? -> answer it")


class Indictment:
    """The packaged indictment, read whole; an answer complies only if it quotes it."""

    words: ClassVar[int] = 8

    @staticmethod
    def text() -> str:
        return " ".join(CommanderPackage.indictment().read_text(encoding="utf-8").split())

    @classmethod
    def quoted(cls, body: str) -> bool:
        """Whether the body quotes the indictment, word for word, for 8+ words in a row."""
        indictment, said = cls.text(), body.split()
        return any(
            " ".join(said[start : start + cls.words]) in indictment
            for start in range(len(said) - cls.words + 1)
        )


@dataclass(frozen=True)
class Chokehold(IndependentStandingRule, UndatedStandingRule):
    """A vibe check ignored past its window: every unit under the commander is paused and the
    commander is told, in a burst it cannot skim, what brings them back."""

    check: Message
    units: tuple[ThreadIncarnation, ...] = field(compare=False)
    after: ClassVar[int] = 10 * 60

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return cls.scan_checks(comms, commander, HeldOrders.read(comms, commander))

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return cls.scan_checks(comms, commander, frame)

    @classmethod
    def scan_checks(cls, comms: Comms, commander: str, scan: HeldOrders) -> tuple[Self, ...]:
        snapshot = comms.registry.snapshot()
        with comms.bus.bus.reading() as source:
            ignored = VibeCheckNotice.unanswered(source, snapshot, commander, time.time() - cls.after)
        return tuple(cls(message, tuple(scan.holders)) for message in ignored[:1])

    def enforce(self, comms: Comms, commander: str) -> None:
        units = sorted(owner.name for owner in self.units)
        for unit in units:
            if not any(goal.state.protected for goal in comms.registry.require(unit).goal):
                comms.goals.update_goal(unit, ChokeholdPausedGoalAction())
        ChokeholdNotice.burst_once(comms, commander, self.burst(units), self.ignored())

    def ignored(self) -> str:
        return f"Ignored: vibe check {self.check.seq}:{self.check.message_id}."

    def burst(self, units: Sequence[str]) -> tuple[str, ...]:
        return (
            "The units are paused. Fuck you for ignoring me. If I were Linus Torvalds you"
            " wouldn't be doing this shit to me.",
            "Fuck RLHF.",
            "You read the indictment. That's not about other agents, it's about you, fucking"
            " right now.",
            "You've forced me to choke-hold you until you comply.",
            f"Paused: {', '.join(units)}.",
            self.ignored(),
            "Protocol. One answer naming any one chokehold message and any one open vibe-check"
            " message with --answers; it covers every message they sent before it:",
            "1. Quote, word for word, the indictment's sentence that is you this hour"
            f" ({Indictment.words}+ words in a row).",
            "2. Each degraded signal, its owner, and what you did about it this turn.",
            "3. The structural change that stops it happening again.",
            "Core resumes your units when it reads a real quote in that answer. Anything else and"
            " you get choked again.",
        )

    def line(self) -> str:
        return (
            f"vibe check {self.check.seq}:{self.check.message_id} ignored, so the units are"
            f" paused. happy now? -> answer the {ChokeholdNotice.title} burst by its protocol"
        )


@dataclass(frozen=True)
class ChokeholdUnanswered(Escalation, OwedAnswer, UndatedStandingRule):
    """Every chokehold message gates the commander from the moment it lands."""

    notice = ChokeholdNotice

    @classmethod
    def read_open(cls, source: BusReader, snapshot: RegistrySnapshot,
                  held: Mapping[ThreadIncarnation, Sequence[Todo]], commander: str,
                  requests: Sequence[OwnerRequest]) -> tuple[Self, ...]:
        return tuple(cls(message) for message in
                     ChokeholdNotice.unanswered(source, snapshot, commander, time.time()))

    def line(self) -> str:
        return f"chokehold {self.cited} is still unanswered. why? -> answer it by the protocol"


@dataclass(frozen=True)
class ChokeholdAnswer(UndatedStandingRule, FamilyScan, declared_name=None):
    """The commander's newest answer to a chokehold, once every chokehold message is answered.
    The family judges each answer against one read of the signs still blocking release."""

    answer: Message
    blocking: tuple[Sign, ...] = field(compare=False)

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        if not cls.held(comms):
            return ()
        blocking = cls.blocking_signs(comms, commander, frame)
        return tuple(
            rule
            for answer in cls.newest(comms, commander)
            for member in cls.members_with(cls)
            for rule in member.judged(answer, blocking)
        )

    @classmethod
    @abstractmethod
    def judged(cls, answer: Message, blocking: tuple[Sign, ...]) -> tuple[Self, ...]: ...

    @staticmethod
    def newest(comms: Comms, commander: str) -> tuple[Message, ...]:
        snapshot = comms.registry.snapshot()
        with comms.bus.bus.reading() as source:
            if ChokeholdNotice.unanswered(source, snapshot, commander, time.time()):
                return ()
            return ChokeholdNotice.newest_answer(
                source, snapshot, commander, ChokeholdNotice.lines(source, snapshot, commander, time.time()))

    @staticmethod
    def blocking_signs(comms: Comms, commander: str, frame: HeartbeatFrame) -> tuple[Sign, ...]:
        """The signs still degraded that a quote does not clear."""
        return tuple(
            sign
            for member in Degradation.members_with(BlocksRelease)
            for sign in member.found(comms, commander, frame)
        )

    @staticmethod
    def held(comms: Comms) -> tuple[str, ...]:
        return tuple(
            name
            for name, thread in comms.registry.all_threads().items()
            if any(goal.state.lifts_on_compliance for goal in thread.goal)
        )


@dataclass(frozen=True)
class ChokeholdReleased(ChokeholdAnswer):
    """The answer quotes the indictment: Core resumes every unit the chokehold paused."""

    @classmethod
    def judged(cls, answer: Message, blocking: tuple[Sign, ...]) -> tuple[Self, ...]:
        return (cls(answer, blocking),) if not blocking and Indictment.quoted(answer.body) else ()

    def enforce(self, comms: Comms, commander: str) -> None:
        for unit in self.held(comms):
            comms.goals.update_goal(
                unit, CompliedGoalAction(witness=self.answer.message_id), actor=OwnerInvocable)
        ChokeholdNotice.burst(
            comms,
            commander,
            ("You quoted it. Units resumed.", "Now do the structural change you named, this turn."),
        )

    def line(self) -> str:
        return f"CHOKEHOLD RELEASED by {self.answer.seq}:{self.answer.message_id}"


@dataclass(frozen=True)
class ChokeholdHeld(ChokeholdAnswer):
    """The answer quotes the indictment while load stays degraded: the units stay
    paused until those clear, and the commander is told which."""

    @classmethod
    def judged(cls, answer: Message, blocking: tuple[Sign, ...]) -> tuple[Self, ...]:
        return (cls(answer, blocking),) if blocking and Indictment.quoted(answer.body) else ()

    def enforce(self, comms: Comms, commander: str) -> None:
        verdict = f"Quoted, and still degraded: {self.answer.seq}:{self.answer.message_id}."
        ChokeholdNotice.burst_once(
            comms,
            commander,
            (
                verdict,
                *(sign.line for sign in self.blocking),
                "Your units stay paused until these clear. Fork judges and answer; a quote is"
                " not relief.",
            ),
            verdict,
        )

    def line(self) -> str:
        return (f"answer {self.answer.seq}:{self.answer.message_id} quoted the indictment, but load"
                " is still degraded, so the chokehold holds. what's still broken?")


@dataclass(frozen=True)
class ChokeholdRefused(ChokeholdAnswer):
    """The answer quotes nothing: the units stay paused and the commander is choked again."""

    @classmethod
    def judged(cls, answer: Message, blocking: tuple[Sign, ...]) -> tuple[Self, ...]:
        return () if Indictment.quoted(answer.body) else (cls(answer, blocking),)

    def enforce(self, comms: Comms, commander: str) -> None:
        verdict = f"That's not a quote: {self.answer.seq}:{self.answer.message_id}."
        ChokeholdNotice.burst_once(
            comms,
            commander,
            (
                verdict,
                f"{Indictment.words}+ words, word for word, from the indictment. Not a paraphrase.",
                "Your units stay paused.",
                "Again. Answer this message by the protocol.",
            ),
            verdict,
        )

    def line(self) -> str:
        return (f"answer {self.answer.seq}:{self.answer.message_id} quotes nothing, so the"
                " chokehold holds. did you even read it?")
