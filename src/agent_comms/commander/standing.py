"""Standing rules read from the commander's own files, members of Core's StandingRule family."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..comms import Comms
from ..standing_rules import ReasonedStandingRule
from . import capacity, judge, rules
from .config import CommanderConfig

CONFIG = CommanderConfig.installed()


@dataclass(frozen=True)
class IdleUnits(ReasonedStandingRule):
    """Units active in the recent log holding no order; parked threads and the commander are not."""

    recent: ClassVar[int] = 6000
    text = "IDLE UNITS with no order: {why} -> give each the top unowned row now"

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        with open(CONFIG.state / "activity.log", errors="replace") as log:
            seen = {line[9:].split(" ", 1)[0] for line in log.readlines()[-cls.recent :]}
        held = {todo.holder for todo in cls.todos(comms, commander) if todo.state.open}
        idle = sorted(seen - held - CONFIG.parked - {commander, ""})
        return ", ".join(idle) or None


@dataclass(frozen=True)
class ForkNow(ReasonedStandingRule):
    text = "FORK NOW: {why}"

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        return capacity.short()


@dataclass(frozen=True)
class Unjudged(ReasonedStandingRule):
    limit: ClassVar[int] = 30
    text = "UNJUDGED {why} edits -> judge or fork a judge slice"

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        total = len(judge.unjudged())
        return str(total) if total > cls.limit else None


@dataclass(frozen=True)
class RulesDue(ReasonedStandingRule):
    text = (
        "RULES DUE ({why}) -> agent-comms-commander rules review"
        " and answer the drawn rules before any order"
    )

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        return rules.stale(commander)

    @classmethod
    def broken_at(cls, comms: Comms, commander: str) -> float | None:
        return rules.stale_since(commander)
