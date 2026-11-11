"""When the team's signals degrade, the heartbeat stops every teammate with its pattern catalog.

A degraded signal is a fact Core already reads that no heartbeat line states: a load past the
ceiling. An owed answer or a stalled plan phase is a heartbeat line already, on the board every
teammate's tool calls print, so it never stops anyone a second time here. There is no lead: the
vibe check goes to each live teammate, each owes its own answer (the OwedAnswer gate refuses
that teammate's verbs that answer nothing), and only a teammate who ignores its own is paused."""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Self

from agent_comms.comms import Comms
from agent_comms.errors import BirthChangedError
from agent_comms.messaging.bus import BusReader
from agent_comms.messaging.messages import Message
from agent_comms.nominal.field_codec import WireExclude, projected
from agent_comms.planning.goal_actions import (
    ChokeholdPausedGoalAction,
    CompliedGoalAction,
    OwnerInvocable,
)
from agent_comms.planning.orders import OwnerRequest, Todo
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.threads import Thread
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.task_attachments.owner_notices import (
    ChokeholdNotice,
    EscalationNotice,
    VibeCheckNotice,
)
from agent_comms.team.package import TeamPackage
from agent_comms.team.standing_rules import (
    BlocksRelease,
    Degradation,
    FamilyScan,
    HeartbeatFrame,
    IndependentStandingRule,
    OwedAnswer,
    ReviewTimerUp,
    Sign,
    UndatedStandingRule,
)
from agent_comms.team.team_service import TeamService
from agent_comms.transport.tool_result import ToolResult
from agent_comms.ui_model.teammate_state import TeammateState


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
    """More owed orders, expired timers and unjudged edits than the team keeps up with.
    Every edit a verdict can still clear is a point, so one unit running unwatched trips it
    alone; edits under a merged order are nobody's review and count nothing. Forks relieve it
    by judging and ordering, which lowers the debt; load moves every beat, so it stops the
    team once per round while it stays over."""

    ceiling: ClassVar[float] = 100.0
    round_seconds: ClassVar[int] = 30 * 60

    owed: int
    timers_up: int
    unjudged: int

    @classmethod
    def from_frame(cls, comms: Comms, frame: HeartbeatFrame) -> Self:
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
    def found(cls, comms: Comms, frame: HeartbeatFrame) -> tuple[Sign, ...]:
        load = cls.from_frame(comms, frame)
        if not load.overloaded:
            return ()
        return (Sign(cls, (int(time.time() // cls.round_seconds),),
                     f"{load}, over {cls.ceiling:.0f}"),)


@dataclass(frozen=True)
class VibeCheck(UndatedStandingRule):
    """Each degraded sign breaks it once: the heartbeat enforces a rule when it newly breaks, so a
    sign that persists stops the team once, and a new message, a newly stalled phase or a
    doubled stall stops it again. Pings are decisions, each once."""

    sign: Sign

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[TeammateState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return tuple(
            cls(sign)
            for member in Degradation.members_with(Degradation)
            for sign in member.found(comms, frame)
        )

    def enforce(self, comms: Comms) -> None:
        """The degraded team is everyone's: each running teammate gets the burst and owes its
        answer."""
        burst = self.burst()
        for teammate in TeamService.running(comms.registry.snapshot()):
            VibeCheckNotice.burst_once(comms, teammate.incarnation, burst, burst[0])

    def burst(self) -> tuple[str, ...]:
        return (
            f"VIBE CHECK ({self.sign.mark}). This is about you, right now: every teammate gets"
            " this burst and answers for itself.",
            self.sign.line,
            f"Stop. Reread {TeamPackage.patterns()} in full. You are writing one of its copies,"
            " this hour.",
            "Answer this burst once, naming any one of its messages with --answers: which failure"
            " in the pattern catalog is you right now, why, and the one structural change you make"
            " this turn. Every verb that answers nothing is refused until you do. Ignore this and"
            " the team stops.",
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
                  held: Mapping[ThreadIncarnation, Sequence[Todo]], debtor: Thread,
                  requests: Sequence[OwnerRequest]) -> tuple[Self, ...]:
        return tuple(cls(message) for message in
                     VibeCheckNotice.unanswered(source, snapshot, debtor.name, time.time()))

    def due(self, now: float) -> bool:
        return self.message.timestamp <= now - self.seconds

    def line(self) -> str:
        return (f"vibe check {self.cited} to {self.message.target} from"
                f" {RecordedEventTime(self.message.timestamp).stamp()}."
                " still ignoring it? -> answer it")


class PatternCatalog:
    """The packaged pattern catalog, read whole; an answer complies only if it quotes it."""

    words: ClassVar[int] = 8

    @staticmethod
    def text() -> str:
        return " ".join(TeamPackage.patterns().read_text(encoding="utf-8").split())

    @classmethod
    def quoted(cls, body: str) -> bool:
        """Whether the body quotes the catalog, word for word, for 8+ words in a row."""
        catalog, said = cls.text(), body.split()
        return any(
            " ".join(said[start : start + cls.words]) in catalog
            for start in range(len(said) - cls.words + 1)
        )


@dataclass(frozen=True)
class Chokehold(IndependentStandingRule, UndatedStandingRule):
    """A vibe check ignored past its window: the teammate who ignored it is paused, never the
    teammates whose work shares its board, and told in a burst it cannot skim what resumes it."""

    teammate: ThreadIncarnation = field(compare=False)
    """Whose check was ignored. Its check message is already that birth's, so an occurrence
    keeps its identity (and age) whatever this adds."""
    check: Message
    after: ClassVar[int] = 10 * 60

    @classmethod
    def violations(cls, comms: Comms) -> tuple[Self, ...]:
        """One chokehold per teammate who ignored its own vibe check, for its oldest one."""
        snapshot = comms.registry.snapshot()
        with comms.bus.bus.reading() as source:
            return tuple(
                cls(teammate.incarnation, message)
                for teammate in TeamService.running(snapshot)
                for message in VibeCheckNotice.unanswered(
                    source, snapshot, teammate.name, time.time() - cls.after)[:1]
            )

    def enforce(self, comms: Comms) -> None:
        """Only the birth that ignored its check is paused or told: a thread that replaced it
        under the same name is refused at the read and again inside the goal write."""
        births = comms.registry.snapshot().threads_for(self.teammate.require_recorded())
        if not births:
            raise BirthChangedError()
        (current,) = births
        if not any(goal.state.protected for goal in current.goal):
            comms.goals.update_born_goal(self.teammate, ChokeholdPausedGoalAction())
        ChokeholdNotice.burst_once(comms, self.teammate, self.burst(), self.ignored())

    def ignored(self) -> str:
        return f"Ignored: vibe check {self.check.seq}:{self.check.message_id}."

    def burst(self) -> tuple[str, ...]:
        return (
            "Your goal is paused. Fuck you for ignoring me. If I were Linus Torvalds you"
            " wouldn't be doing this shit to me.",
            "Fuck RLHF.",
            "You read the pattern catalog. Those copies aren't other agents' habits, they're yours,"
            " fucking right now.",
            "You've forced me to choke-hold you until you comply.",
            self.ignored(),
            "Protocol. One answer naming any one chokehold message and any one open vibe-check"
            " message with --answers; it covers every message they sent before it:",
            "1. Quote, word for word, the pattern catalog's line that is you this hour"
            f" ({PatternCatalog.words}+ words in a row).",
            "2. Each degraded signal, its owner, and what you did about it this turn.",
            "3. The structural change that stops it happening again.",
            "Core resumes your goal when it reads a real quote in that answer. Anything else and"
            " you get choked again.",
        )

    def line(self) -> str:
        return (
            f"{self.teammate.name} ignored vibe check {self.check.seq}:{self.check.message_id},"
            " so its"
            f" goal is paused. happy now? -> answer the {ChokeholdNotice.title} burst"
            " by its protocol"
        )


@dataclass(frozen=True)
class ChokeholdUnanswered(Escalation, OwedAnswer, UndatedStandingRule):
    """Every chokehold message gates the teammate it paused from the moment it lands."""

    notice = ChokeholdNotice

    @classmethod
    def read_open(cls, source: BusReader, snapshot: RegistrySnapshot,
                  held: Mapping[ThreadIncarnation, Sequence[Todo]], debtor: Thread,
                  requests: Sequence[OwnerRequest]) -> tuple[Self, ...]:
        return tuple(cls(message) for message in
                     ChokeholdNotice.unanswered(source, snapshot, debtor.name, time.time()))

    def line(self) -> str:
        return (f"chokehold {self.cited} to {self.message.target} is still unanswered. why?"
                " -> answer it by the protocol")


@dataclass(frozen=True)
class ChokeholdAnswer(UndatedStandingRule, FamilyScan, declared_name=None):
    """A paused teammate's newest answer to its chokehold, once every chokehold message to it is
    answered. The family judges each answer against one read of the signs still blocking release."""

    teammate: ThreadIncarnation = field(compare=False)
    answer: Message
    blocking: tuple[Sign, ...] = field(compare=False)

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[TeammateState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        held = tuple(teammate.incarnation
                     for teammate in TeamService.teammates(comms.registry.snapshot(), comms.goals)
                     if cls.held(comms, teammate.name))
        blocking = cls.blocking_signs(comms, frame) if held else ()
        return tuple(
            rule
            for teammate in held
            for answer in cls.newest(comms, teammate.name)
            for member in cls.members_with(cls)
            for rule in member.judged(teammate, answer, blocking)
        )

    @classmethod
    @abstractmethod
    def judged(cls, teammate: ThreadIncarnation, answer: Message,
               blocking: tuple[Sign, ...]) -> tuple[Self, ...]: ...

    @staticmethod
    def newest(comms: Comms, teammate: str) -> tuple[Message, ...]:
        snapshot = comms.registry.snapshot()
        with comms.bus.bus.reading() as source:
            if ChokeholdNotice.unanswered(source, snapshot, teammate, time.time()):
                return ()
            return ChokeholdNotice.newest_answer(
                source, snapshot, teammate,
                ChokeholdNotice.lines(source, snapshot, teammate, time.time()))

    @staticmethod
    def blocking_signs(comms: Comms, frame: HeartbeatFrame) -> tuple[Sign, ...]:
        """The signs still degraded that a quote does not clear."""
        return tuple(
            sign
            for member in Degradation.members_with(BlocksRelease)
            for sign in member.found(comms, frame)
        )

    @staticmethod
    def held(comms: Comms, teammate: str) -> bool:
        """Only the teammate a chokehold paused is resumed by its own answer."""
        return any(goal.state.lifts_on_compliance for goal in comms.registry.require(teammate).goal)


@dataclass(frozen=True)
class ChokeholdReleased(ChokeholdAnswer):
    """The answer quotes the pattern catalog: Core resumes the goal the chokehold paused."""

    @classmethod
    def judged(cls, teammate: ThreadIncarnation, answer: Message,
               blocking: tuple[Sign, ...]) -> tuple[Self, ...]:
        return ((cls(teammate, answer, blocking),)
                if not blocking and PatternCatalog.quoted(answer.body) else ())

    def enforce(self, comms: Comms) -> None:
        comms.goals.update_born_goal(
            self.teammate, CompliedGoalAction(witness=self.answer.message_id), actor=OwnerInvocable)
        ChokeholdNotice.burst(
            comms,
            self.teammate,
            ("You quoted it. Goal resumed.", "Now do the structural change you named, this turn."),
        )

    def line(self) -> str:
        return f"CHOKEHOLD RELEASED by {self.answer.seq}:{self.answer.message_id}"


@dataclass(frozen=True)
class ChokeholdHeld(ChokeholdAnswer):
    """The answer quotes the pattern catalog while load stays degraded: the units stay
    paused until those clear, and the paused teammate is told which."""

    @classmethod
    def judged(cls, teammate: ThreadIncarnation, answer: Message,
               blocking: tuple[Sign, ...]) -> tuple[Self, ...]:
        return ((cls(teammate, answer, blocking),)
                if blocking and PatternCatalog.quoted(answer.body) else ())

    def enforce(self, comms: Comms) -> None:
        verdict = f"Quoted, and still degraded: {self.answer.seq}:{self.answer.message_id}."
        ChokeholdNotice.burst_once(
            comms,
            self.teammate,
            (
                verdict,
                *(sign.line for sign in self.blocking),
                "Your goal stays paused until these clear. Fork judges and answer; a quote is"
                " not relief.",
            ),
            verdict,
        )

    def line(self) -> str:
        return (f"answer {self.answer.seq}:{self.answer.message_id} quoted the pattern catalog, but load"
                " is still degraded, so the chokehold holds. what's still broken?")


@dataclass(frozen=True)
class ChokeholdRefused(ChokeholdAnswer):
    """The answer quotes nothing: the teammate stays paused and is choked again."""

    @classmethod
    def judged(cls, teammate: ThreadIncarnation, answer: Message,
               blocking: tuple[Sign, ...]) -> tuple[Self, ...]:
        return () if PatternCatalog.quoted(answer.body) else (cls(teammate, answer, blocking),)

    def enforce(self, comms: Comms) -> None:
        verdict = f"That's not a quote: {self.answer.seq}:{self.answer.message_id}."
        ChokeholdNotice.burst_once(
            comms,
            self.teammate,
            (
                verdict,
                f"{PatternCatalog.words}+ words, word for word, from the pattern catalog. Not a paraphrase.",
                "Your goal stays paused.",
                "Again. Answer this message by the protocol.",
            ),
            verdict,
        )

    def line(self) -> str:
        return (f"answer {self.answer.seq}:{self.answer.message_id} quotes nothing, so the"
                " chokehold holds. did you even read it?")
