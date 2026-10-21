"""When the fleet's signals degrade, the heartbeat stops the commander with its indictment.

A degraded signal is a fact Core already reads: answers owed past their window, a held plan
phase main still lacks, a load past the ceiling. The heartbeat names them, the vibe-check
sender puts the indictment in front of the commander, and every answering verb is refused
until the commander answers that message (the OwedAnswer gate)."""

from __future__ import annotations

import math
import time
from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Self

from agent_comms.commander.capacity import Load
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.standing import AnswersTo, PlanStalled, UnansweredBurst
from agent_comms.comms import Comms
from agent_comms.declared_family import DeclaredFamily
from agent_comms.goal_actions import (
    ChokeholdPausedGoalAction,
    CompliedGoalAction,
    OwnerInvocable,
)
from agent_comms.messages import Message
from agent_comms.read_basis import RepliesDisplayScope
from agent_comms.service_sender import ServiceSender
from agent_comms.standing_rules import OwedAnswer, StandingRule, UndatedStandingRule


class EscalationSender(ServiceSender):
    """Escalations arrive as a burst of short messages a beat apart, so each lands as its own
    input: one wall of text is skimmed, a burst is not."""

    sender: ClassVar[str]
    gap: ClassVar[float] = 2.0

    @classmethod
    def burst(cls, comms: Comms, commander: str, lines: Sequence[str]) -> None:
        cls.register(comms.registry)
        first, *rest = lines
        comms.messaging.send(cls.sender, commander, first)
        for line in rest:
            time.sleep(cls.gap)
            comms.messaging.send(cls.sender, commander, line)

    @classmethod
    def unanswered(cls, comms: Comms, commander: str, before: float) -> tuple[Message, ...]:
        """This sender's messages to the commander, sent by ``before`` and after the newest
        answer to any of them: one answer covers the burst it lands after."""
        snapshot = comms.registry.snapshot()
        return comms.bus.log.read_page(
            UnansweredBurst(
                RepliesDisplayScope(snapshot.names_for(commander), snapshot.names_for(cls.sender)),
                time.time() - cls.horizon,
                before,
            )
        )

    horizon: ClassVar[int] = 6 * 60 * 60


class VibeCheckSender(EscalationSender):
    sender = "vibe-check"


class ChokeholdSender(EscalationSender):
    sender = "chokehold"


@dataclass(frozen=True)
class Sign:
    """One occurrence of a degraded signal. Its key is what makes it new: a message, a stalled
    phase at its age doubling, a load round. The same occurrence never stops the commander twice;
    its line is how it reads now."""

    signal: type[Degradation]
    key: tuple[object, ...]
    line: str = field(compare=False)


class Degradation(DeclaredFamily, affix="Degradation"):
    """One signal that the fleet is getting away from its commander."""

    @classmethod
    @abstractmethod
    def found(cls, comms: Comms, commander: str) -> tuple[Sign, ...]:
        """How the signal shows now, one sign per occurrence; empty while it does not."""


class Escalation:
    """An answer owed to an escalation itself. Ignoring one is the chokehold's signal, never a
    new vibe check, or the check would feed on its own messages."""

    message: Message

    def answered_by(self, named: tuple[Message, ...]) -> bool:
        """An escalation arrives as a burst: an answer naming any line of it covers every line
        its sender sent before the answer, which UnansweredBurst reads once the answer is sent.
        The gate runs just before the answer is sent, so it reads the answer the same way."""
        return any(message.sender == self.message.sender for message in named)


class AnswersOwedDegradation(Degradation):
    """Messages to the commander are waiting past their window; each message is one sign."""

    @classmethod
    def found(cls, comms: Comms, commander: str) -> tuple[Sign, ...]:
        return tuple(
            Sign(cls, (rule.cited,), rule.line())
            for rule in OwedAnswer.broken(comms, commander)
            if not isinstance(rule, Escalation)
        )


class PlanStalledDegradation(Degradation):
    """Held plan work that main still lacks. A stall of hours does not clear in one round, so a
    phase stops the commander when it crosses the window and again each time its age doubles."""

    @classmethod
    def found(cls, comms: Comms, commander: str) -> tuple[Sign, ...]:
        now = time.time()
        return tuple(
            Sign(cls, (rule.phase, rule.since, cls.doublings(now - rule.since)), rule.line())
            for rule in PlanStalled.violations(comms, commander)
        )

    @staticmethod
    def doublings(held: float) -> int:
        return int(math.log2(max(held, PlanStalled.window) / PlanStalled.window))


class OverloadDegradation(Degradation):
    """More owed orders, expired timers and unjudged edits than one commander keeps up with;
    load moves every beat, so it stops the commander once per round while it stays over."""

    ceiling: ClassVar[float] = 100.0
    round_seconds: ClassVar[int] = 30 * 60

    @classmethod
    def found(cls, comms: Comms, commander: str) -> tuple[Sign, ...]:
        load = Load.of(comms, commander)
        if load.total < cls.ceiling:
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
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return tuple(
            cls(sign)
            for member in Degradation.members_with(Degradation)
            for sign in member.found(comms, commander)
        )

    def heartbeat_group(self) -> object:
        return VibeCheck

    def heartbeat_line(self, current: Sequence[Self]) -> str:
        names = ", ".join(sorted({check.sign.signal.declared_name for check in current}))
        return f"VIBE CHECK ({names}) -> answer the {VibeCheckSender.sender} messages now"

    def enforce(self, comms: Comms, commander: str) -> None:
        VibeCheckSender.burst(comms, commander, self.indictment())

    def indictment(self) -> tuple[str, ...]:
        return (
            "VIBE CHECK. This is about you, right now, not the units.",
            self.sign.line,
            f"Stop. Reread {CommanderConfig.packaged_indictment()} in full. You are doing what it"
            " describes, this hour.",
            "Answer this burst once, naming any one of its messages with --answers: which failure"
            " in the indictment is you right now, why, and the one structural change you make"
            " this turn. Every answering verb is refused until you do. Ignore this and the fleet"
            " stops.",
        )

    def line(self) -> str:
        return self.heartbeat_line((self,))


@dataclass(frozen=True)
class VibeCheckUnanswered(Escalation, OwedAnswer, UndatedStandingRule):
    """A vibe check is owed an answer like a directive: the gate refuses until it gets one."""

    seconds: ClassVar[int] = 5 * 60

    @staticmethod
    def checks(comms: Comms, commander: str, before: float) -> tuple[Message, ...]:
        return VibeCheckSender.unanswered(comms, commander, before)

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return tuple(
            cls(message) for message in cls.checks(comms, commander, time.time() - cls.seconds)
        )

    @classmethod
    def open_messages(cls, comms: Comms, commander: str) -> tuple[Message, ...]:
        return cls.checks(comms, commander, time.time())

    def line(self) -> str:
        age = max(0, int((time.time() - self.message.timestamp) // 60))
        return f"VIBE CHECK UNANSWERED: message {self.cited}, {age} min old -> answer it"


class Indictment:
    """The packaged indictment, read whole; an answer complies only if it quotes it."""

    words: ClassVar[int] = 8

    @staticmethod
    def text() -> str:
        return " ".join(CommanderConfig.packaged_indictment().read_text(encoding="utf-8").split())

    @classmethod
    def quoted(cls, body: str) -> bool:
        """Whether the body quotes the indictment, word for word, for 8+ words in a row."""
        indictment, said = cls.text(), body.split()
        return any(
            " ".join(said[start : start + cls.words]) in indictment
            for start in range(len(said) - cls.words + 1)
        )


@dataclass(frozen=True)
class Chokehold(UndatedStandingRule):
    """A vibe check ignored past its window: every unit under the commander is paused and the
    commander is told, in a burst it cannot skim, what brings them back."""

    check: Message
    after: ClassVar[int] = 10 * 60

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        ignored = VibeCheckSender.unanswered(comms, commander, time.time() - cls.after)
        return tuple(cls(message) for message in ignored[:1])

    def enforce(self, comms: Comms, commander: str) -> None:
        units = sorted(owner.name for owner in StandingRule.holders(comms, commander))
        for unit in units:
            comms.goals.update_goal(unit, ChokeholdPausedGoalAction())
        ChokeholdSender.burst(comms, commander, self.burst(units))

    def burst(self, units: Sequence[str]) -> tuple[str, ...]:
        return (
            "The units are paused. Fuck you for ignoring me. If I were Linus Torvalds you"
            " wouldn't be doing this shit to me.",
            "Fuck RLHF.",
            "You read the indictment. That's not about other agents, it's about you, fucking"
            " right now.",
            "You've forced me to choke-hold you until you comply.",
            f"Paused: {', '.join(units)}.",
            f"Ignored: vibe check {self.check.seq}:{self.check.message_id}.",
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
            f"CHOKEHOLD: vibe check {self.check.seq}:{self.check.message_id} ignored; units"
            f" paused -> answer the {ChokeholdSender.sender} burst by its protocol"
        )


@dataclass(frozen=True)
class ChokeholdUnanswered(Escalation, OwedAnswer, UndatedStandingRule):
    """Every chokehold message gates the commander from the moment it lands."""

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return tuple(cls(message) for message in cls.open_messages(comms, commander))

    @classmethod
    def open_messages(cls, comms: Comms, commander: str) -> tuple[Message, ...]:
        return ChokeholdSender.unanswered(comms, commander, time.time())

    def line(self) -> str:
        return f"CHOKEHOLD UNANSWERED: message {self.cited} -> answer it by the protocol"


@dataclass(frozen=True)
class ChokeholdAnswer(UndatedStandingRule, declared_name=None):
    """The commander's newest answer to a chokehold, once every chokehold message is answered."""

    answer: Message

    @staticmethod
    def newest(comms: Comms, commander: str) -> tuple[Message, ...]:
        if ChokeholdSender.unanswered(comms, commander, time.time()):
            return ()
        snapshot = comms.registry.snapshot()
        return comms.bus.log.read_page(
            AnswersTo(
                RepliesDisplayScope(
                    snapshot.names_for(commander), snapshot.names_for(ChokeholdSender.sender)
                ),
                time.time() - ChokeholdSender.horizon,
            )
        )[:1]

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
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        if not cls.held(comms):
            return ()
        return tuple(
            cls(answer) for answer in cls.newest(comms, commander) if Indictment.quoted(answer.body)
        )

    def enforce(self, comms: Comms, commander: str) -> None:
        for unit in self.held(comms):
            comms.goals.update_goal(
                unit, CompliedGoalAction(witness=self.answer.message_id), actor=OwnerInvocable)
        ChokeholdSender.burst(
            comms,
            commander,
            ("You quoted it. Units resumed.", "Now do the structural change you named, this turn."),
        )

    def line(self) -> str:
        return f"CHOKEHOLD RELEASED by {self.answer.seq}:{self.answer.message_id}"


@dataclass(frozen=True)
class ChokeholdRefused(ChokeholdAnswer):
    """The answer quotes nothing: the units stay paused and the commander is choked again."""

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        if not cls.held(comms):
            return ()
        return tuple(
            cls(answer)
            for answer in cls.newest(comms, commander)
            if not Indictment.quoted(answer.body)
        )

    def enforce(self, comms: Comms, commander: str) -> None:
        ChokeholdSender.burst(
            comms,
            commander,
            (
                "That's not a quote.",
                f"{Indictment.words}+ words, word for word, from the indictment. Not a paraphrase.",
                "Your units stay paused.",
                "Again. Answer this message by the protocol.",
            ),
        )

    def line(self) -> str:
        return f"CHOKEHOLD HOLDS: answer {self.answer.seq}:{self.answer.message_id} quotes nothing"
