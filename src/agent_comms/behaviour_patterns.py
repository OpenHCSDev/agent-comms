"""Behaviour failure patterns Core catches: each declared once with its signature and check.

A member names what the pattern looks like in state, readable without trusting
a report, the existing checks that enforce it, and where the pattern was learned.
``docs/agents/behaviour-catalog.md`` is this family's ``catalog()``; regenerate it
with ``python -m agent_comms.behaviour_patterns > docs/agents/behaviour-catalog.md``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from agent_comms.commander.standing import PlanIdle, PlanStalled

from .child_process import CountPrinted, NoIntegerPrinted
from .commander.order import CheckRequest
from .declared_family import DeclaredFamily
from .pattern_sources import (
    CeremonyFailure,
    CommanderRule,
    HeldPlanFailure,
    IntentionsAsAccomplishmentsFailure,
    MeasuredWrongThingFailure,
    PatternSource,
    PostmortemFailure,
    ProblemFix,
    SeedCatalog,
)
from .standing_rules import BoardDisagrees, StandingRule
from .todos import PlainText, Todo, TodoStore, VoidStrikeTodo

POSTMORTEM = "https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md"


class PatternCheck(ABC):
    """An existing check that enforces a pattern."""

    @abstractmethod
    def cite(self) -> str: ...


@dataclass(frozen=True)
class RuleCheck(PatternCheck):
    """A StandingRule member that reports the pattern on every read of state."""

    rule: type[StandingRule]

    def cite(self) -> str:
        where = f"{self.rule.__module__}.{self.rule.__qualname__}"
        return f"standing rule `{self.rule.declared_name}` (`{where}`)"


@dataclass(frozen=True)
class CallableCheck(PatternCheck):
    """The existing implementation that checks state or refuses the action."""

    check: Callable[..., object]

    def cite(self) -> str:
        return f"check in `{self.check.__module__}.{self.check.__qualname__}`"


@dataclass(frozen=True)
class PendingCheck(PatternCheck):
    """A check absent from this checkout; its implementation replaces this citation."""

    problem: str
    change: str

    def cite(self) -> str:
        return f"not enforced here: {self.problem} ({self.change})"


class BehaviourPattern(DeclaredFamily, affix="Pattern"):
    signature: ClassVar[str]
    checks: ClassVar[tuple[PatternCheck, ...]]
    sources: ClassVar[tuple[PatternSource, ...]]

    @classmethod
    def section(cls) -> str:
        checks = "".join(f"- {check.cite()}\n" for check in cls.checks)
        sources = "; ".join(source.cite() for source in cls.sources)
        return (
            f"## {cls.declared_name}\n\n{cls.signature}\n\n"
            f"Checked by:\n\n{checks}\nLearned from: {sources}\n"
        )

    @classmethod
    def catalog(cls) -> str:
        members = cls.members_with(BehaviourPattern)
        failures = "".join(
            f"- Failure {failure.number}, {failure.title}: "
            + (
                ", ".join(
                    f"`{member.declared_name}`"
                    for member in members
                    if any(isinstance(source, failure) for source in member.sources)
                )
                or "no pattern cites it yet"
            )
            + "\n"
            for failure in PostmortemFailure.members_with(PostmortemFailure)
        )
        return (
            "# Behaviour catalog\n\n"
            "Generated from `agent_comms.behaviour_patterns` by\n"
            "`python -m agent_comms.behaviour_patterns > docs/agents/behaviour-catalog.md`;"
            " do not edit by hand.\n\n"
            "Each behaviour failure pattern is declared once in Core with its signature (what it"
            " looks like in state, readable without trusting a report) and the existing checks"
            " that enforce it. Patterns in code, rather than in behaviour, are the refactor-audit"
            " skill's catalog (`refactor-audit/references/patterns/README.md`).\n\n"
            "A pending check records a known gap, not enforcement. Adding a pattern means"
            " adding one `BehaviourPattern` subclass; membership and this document derive"
            " from those classes. `tests/test_behaviour_catalog.py` rejects hand edits.\n\n"
            f"The [Codex postmortem]({POSTMORTEM}) and October 19 commander incidents record"
            " these failures; coverage below derives"
            " from the patterns that cite them:\n\n"
            f"{failures}\n" + "\n".join(member.section() for member in members)
        )


class PlanIdlePattern(BehaviourPattern):
    signature = (
        "A plan phase whose earlier phases are all done is not done itself, and no open"
        " order held by a unit serves it."
    )
    checks = (RuleCheck(PlanIdle),)
    sources = (ProblemFix("P83", "34ba68c81"),)


class PlanStalledPattern(BehaviourPattern):
    signature = (
        "A plan phase whose earlier phases are all done is held by a unit, and main still lacks"
        " it two hours after the phase was first ordered."
    )
    checks = (RuleCheck(PlanStalled),)
    sources = (HeldPlanFailure(),)


class BoardDisagreesPattern(BehaviourPattern):
    signature = (
        "An open order's progress reads no commit while a pull request linked to it has merged."
    )
    checks = (RuleCheck(BoardDisagrees),)
    sources = (ProblemFix("P170", "c9c0c04db"), IntentionsAsAccomplishmentsFailure())


class PlainTextPattern(BehaviourPattern):
    signature = (
        "A commander order, strike, roast or directive whose words outside cited spans match"
        " the declared jargon list in `PlainText`."
    )
    checks = (CallableCheck(PlainText),)
    sources = (CommanderRule("Hate jargon."), CeremonyFailure())


class RepeatedStrikePattern(BehaviourPattern):
    signature = "A send-back strike on an order already struck since it was sent."
    checks = (CallableCheck(Todo.struck_on_order),)
    sources = (ProblemFix("P184", "7af14a520"),)


class StrikeInErrorPattern(BehaviourPattern):
    signature = (
        "A strike recorded in error is deleted, or voided while not live, instead of a void"
        " recorded beside it with a plain reason."
    )
    checks = (CallableCheck(VoidStrikeTodo.apply),)
    sources = (ProblemFix("P47", "15d435152"),)


class UncountedCheckPattern(BehaviourPattern):
    signature = (
        "An order whose check line does not read `repo :: command -> integer`, prints no"
        " integer, or already prints its target before any work."
    )
    checks = (
        CallableCheck(CheckRequest.parse),
        CallableCheck(NoIntegerPrinted.unproven),
        CallableCheck(CountPrinted.unproven),
    )
    sources = (
        ProblemFix("P114", "d889df612"),
        MeasuredWrongThingFailure(),
        IntentionsAsAccomplishmentsFailure(),
    )


class StrikeOnNamedProblemPattern(BehaviourPattern):
    signature = (
        "A strike recorded on a problem other than the one given as its argument, such"
        " as one moved by a problem number inside its reason. Hunts are commander records,"
        " not strikes on a problem."
    )
    checks = (CallableCheck(TodoStore.apply),)
    sources = (ProblemFix("P194", "b26843727"), CommanderRule("Two strikes on one task take it away."))


class ReportInsteadOfStatePattern(BehaviourPattern):
    signature = "A strike or send-back whose reason cites no state the record can check."
    checks = (PendingCheck("P198", "#1744"),)
    sources = (SeedCatalog(1),)


class WrongMeasurePattern(BehaviourPattern):
    signature = (
        "A done report that names its own measure instead of what its order's Check printed."
    )
    checks = (PendingCheck("P197", "#1733"),)
    sources = (SeedCatalog(4), MeasuredWrongThingFailure())


class CommanderOverloadedPattern(BehaviourPattern):
    signature = (
        "The commander's unread backlog is over the limit while no second commander is forked"
        " to share it."
    )
    checks = (PendingCheck("P199", "#1750, closed without merge"),)
    sources = (SeedCatalog(15),)


class DeadProviderPattern(BehaviourPattern):
    signature = (
        "A unit's latest turn ended in a usage or credit limit on its own model and the unit"
        " still runs on that model."
    )
    checks = (PendingCheck("P200", "#1746, closed without merge"),)
    sources = (SeedCatalog(16),)


class UnownedWorkPattern(BehaviourPattern):
    signature = (
        "Agent-comms work runs under no open order, or on a runtime that publication replaced."
    )
    checks = (PendingCheck("P201", "#1739"),)
    sources = (SeedCatalog(18),)


if __name__ == "__main__":
    print(BehaviourPattern.catalog(), end="")
