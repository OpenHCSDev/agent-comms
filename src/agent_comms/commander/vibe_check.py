"""When the fleet's signals degrade, the heartbeat stops the commander with its indictment.

A degraded signal is a fact Core already reads: answers owed past their window, a held plan
phase main still lacks, a load past the ceiling. The heartbeat names them, the vibe-check
sender puts the indictment in front of the commander, and every answering verb is refused
until the commander answers that message (the OwedAnswer gate)."""

from __future__ import annotations

import time
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Self

from agent_comms.commander.capacity import Load
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.standing import PlanStalled, UnansweredUnitAsks
from agent_comms.comms import Comms
from agent_comms.declared_family import DeclaredFamily
from agent_comms.messages import Message
from agent_comms.read_basis import RepliesDisplayScope
from agent_comms.service_sender import ServiceSender
from agent_comms.standing_rules import OwedAnswer, UndatedStandingRule


class VibeCheckSender(ServiceSender):
    sender = "vibe-check"


class Degradation(DeclaredFamily, affix="Degradation"):
    """One signal that the fleet is getting away from its commander."""

    @classmethod
    @abstractmethod
    def found(cls, comms: Comms, commander: str) -> tuple[str, ...]:
        """How the signal shows now, one line each; empty while it does not."""


class AnswersOwedDegradation(Degradation):
    """Messages to the commander are waiting past their window."""

    @classmethod
    def found(cls, comms: Comms, commander: str) -> tuple[str, ...]:
        return tuple(rule.line() for rule in OwedAnswer.broken(comms, commander))


class PlanStalledDegradation(Degradation):
    """Held plan work that main still lacks."""

    @classmethod
    def found(cls, comms: Comms, commander: str) -> tuple[str, ...]:
        return tuple(rule.line() for rule in PlanStalled.violations(comms, commander))


class OverloadDegradation(Degradation):
    """More owed orders, expired timers and unjudged edits than one commander keeps up with."""

    ceiling: ClassVar[float] = 100.0

    @classmethod
    def found(cls, comms: Comms, commander: str) -> tuple[str, ...]:
        load = Load.of(comms, commander)
        return (f"{load}, over {cls.ceiling:.0f}",) if load.total >= cls.ceiling else ()


@dataclass(frozen=True)
class VibeCheck(UndatedStandingRule):
    """Degraded signals break it once per combination and per round, so a commander still
    degraded half an hour later is stopped again."""

    signals: frozenset[type[Degradation]]
    round: int
    lines: tuple[str, ...] = field(compare=False)
    round_seconds: ClassVar[int] = 30 * 60

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        found = {
            member: member.found(comms, commander)
            for member in Degradation.members_with(Degradation)
        }
        showing = {member: lines for member, lines in found.items() if lines}
        if not showing:
            return ()
        return (
            cls(
                frozenset(showing),
                int(time.time() // cls.round_seconds),
                tuple(line for lines in showing.values() for line in lines),
            ),
        )

    def enforce(self, comms: Comms, commander: str) -> None:
        VibeCheckSender.register(comms.registry)
        comms.messaging.send(VibeCheckSender.sender, commander, self.indictment())

    def indictment(self) -> str:
        return "\n".join(
            (
                "VIBE CHECK. This is about you, right now, not the units:",
                *(f"- {line}" for line in self.lines),
                f"Stop. Reread {CommanderConfig.packaged_indictment()} in full: you are doing what"
                " it describes, this hour. Then answer this message with --answers: which failure"
                " in it is you right now, why it happened, and the one structural change you make"
                " this turn. Every answering verb is refused until you do.",
            )
        )

    def line(self) -> str:
        names = ", ".join(sorted(signal.declared_name for signal in self.signals))
        return f"VIBE CHECK ({names}) -> answer the {VibeCheckSender.sender} message now"


@dataclass(frozen=True)
class VibeCheckUnanswered(OwedAnswer, UndatedStandingRule):
    """A vibe check is owed an answer like a directive: the gate refuses until it gets one."""

    seconds: ClassVar[int] = 5 * 60
    horizon: ClassVar[int] = 6 * 60 * 60

    @staticmethod
    def checks(comms: Comms, commander: str, before: float) -> tuple[Message, ...]:
        snapshot = comms.registry.snapshot()
        return comms.bus.log.read_page(
            UnansweredUnitAsks(
                RepliesDisplayScope(
                    snapshot.names_for(commander), snapshot.names_for(VibeCheckSender.sender)
                ),
                time.time() - VibeCheckUnanswered.horizon,
                before,
            )
        )

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
