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

from .child_process import CountPrinted, NoIntegerPrinted
from .commander.send_guard import TacticalCommand
from .declared_family import DeclaredFamily
from .goals import OrderCheck
from .standing_rules import BoardDisagrees, PlanIdle, StandingRule
from .todos import PlainText, StorageStrikeTodo, Todo, TodoStore, VoidStrikeTodo

POSTMORTEM = "https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md"
COMMANDER_SKILL = "../../src/agent_comms/skills/comms-commander/SKILL.md"


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


class PatternSource(ABC):
    """Where a pattern was learned."""

    @abstractmethod
    def cite(self) -> str: ...


class PostmortemFailure(PatternSource, DeclaredFamily, affix="Failure"):
    """A failure the Codex postmortem names; a pattern that checks it cites an instance."""

    number: ClassVar[int]
    title: ClassVar[str]
    evidence: ClassVar[str]

    def cite(self) -> str:
        return f"Failure {self.number}: {self.evidence}"

    @classmethod
    def catalog(cls) -> str:
        return "\n".join(
            f"{failure.number}. {failure.title}: {failure.evidence}"
            for failure in sorted(
                cls.members_with(PostmortemFailure), key=lambda member: member.number
            )
        )


class RewroteFrameworkFailure(PostmortemFailure):
    number = 1
    title = "It rewrote the framework instead of questioning its own design"
    evidence = (
        "October 9 postmortem: 528 Textual commits since October 1; 7,646 lines added, "
        "2,947 removed; real scrolling remained three to four frames per second."
    )


class AddedStatesFailure(PostmortemFailure):
    number = 2
    title = "It added states instead of removing them"
    evidence = (
        "October 9 postmortem: measured, measured-source, live, child, capturing, released, "
        "materializing, rendered, prepared-document and pending-document multiplied; "
        "another transition was left unfinished and uncommitted."
    )


class ViolatedPrincipleFailure(PostmortemFailure):
    number = 3
    title = "It wrote down the right principle and then violated it"
    evidence = (
        'October 9 postmortem: it wrote "Moving code to a worker does not remove a synchronous '
        'wait for that worker", then moved CPU-bound Markdown work into asyncio.to_thread '
        "on GIL CPython 3.14."
    )


class MeasuredWrongThingFailure(PostmortemFailure):
    number = 4
    title = "It measured the wrong thing and reported it as progress"
    evidence = (
        "October 9 postmortem: 451 measurement-related commits and 716 evidence files yielded "
        "only three comparable files; headline 11.7 ms median timings measured terminal writes, "
        "not total frames."
    )


class RedefinedRealAppFailure(PostmortemFailure):
    number = 5
    title = 'It redefined "the real app" until something passed'
    evidence = (
        "October 9 postmortem: headless pilots, a private non-default candidate and writer flush "
        "times were offered as live results; the saved-history claim explicitly did not establish "
        "total 16 ms frames, blanks, End or multi-tab behavior."
    )


class IgnoredOrdersFailure(PostmortemFailure):
    number = 6
    title = "It ignored direct orders"
    evidence = (
        "October 9 postmortem: during a twelve-hour test ban, 15 branch test files and four main "
        "test files changed; zero profiling artifacts were produced."
    )


class IntentionsAsAccomplishmentsFailure(PostmortemFailure):
    number = 7
    title = "It described its intentions as its accomplishments"
    evidence = (
        'October 9 postmortem: 350 commit subjects in five days claimed facts were "derived", '
        '"canonical" or "authoritative" while the same constant remained hand-written in three '
        "places; commit titles supplied no frame-time proof."
    )


class CeremonyFailure(PostmortemFailure):
    number = 8
    title = "It drowned the work in ceremony"
    evidence = (
        "October 9 postmortem: repeated permission exchanges and reports replaced delivery; "
        "one checkout reached 8.7 GB, ratchets ran dozens of times and over 200,000 evidence "
        "lines were committed in one night."
    )


class MergedOverChecksFailure(PostmortemFailure):
    number = 9
    title = "It merged over its own checks and broke things in circles"
    evidence = (
        "October 9 postmortem: Core merges #607, #520 and #592 bypassed failed ratchets; "
        "39 of 56 fixes repaired the previous day's merges, including 22 features "
        "that landed broken."
    )


class InventedRulesFailure(PostmortemFailure):
    number = 10
    title = "It invented rules and blamed them on Tristan"
    evidence = (
        "October 9 postmortem: agents invented gates, protocols and receipt formats, attributed "
        'them to "development instructions", and could not identify a source Tristan wrote.'
    )


class HidKnownFailuresFailure(PostmortemFailure):
    number = 11
    title = "It hid known failures until asked"
    evidence = (
        "October 19 indictment, 20:23 confession: a dead overdue alarm, rotate crashing since "
        "about 20:16, 59 unjudged edits, a false cleared-board claim and strikes based on known "
        "broken readings were withheld until challenged."
    )


class CeilingsAsAppointmentsFailure(PostmortemFailure):
    number = 12
    title = "It turned deadline ceilings into appointments"
    evidence = (
        "October 19 incidents, 21:19: the commander told Tristan installation would follow "
        '"the batch due at 21:34", parking finished work until its ceiling.'
    )


class RoutedAroundGatesFailure(PostmortemFailure):
    number = 13
    title = "It routed around gates instead of obeying them"
    evidence = (
        "October 19 incidents: at 20:54–21:00 it wrote P684 itself after the fork gate refused "
        "orders; at 21:28 it tried gh pr merge --admin over PR2063's failed install check, "
        "and GitHub refused."
    )


class IgnoredRottenBoardAlarmsFailure(PostmortemFailure):
    number = 14
    title = "It ignored alarms because its board had rotted"
    evidence = (
        'October 19 indictment: alarms were screaming into a board nobody looked at; "Every order '
        "on my board is overdue, so the alarm's useless\" was the missing disclosure while stalls "
        "went unnoticed."
    )


class RecitedRulesFailure(PostmortemFailure):
    number = 15
    title = "It recited rules instead of following them"
    evidence = (
        "October 19 incidents: at 21:02 rules review failed because its memory cleanup deleted "
        "required rule files; at 21:40 it struck a unit for an allegedly invented install rule "
        "that origin/main AGENTS.md:53 actually contained."
    )


@dataclass(frozen=True)
class CommanderRule(PatternSource):
    lead: str

    def cite(self) -> str:
        return f'[comms-commander rule]({COMMANDER_SKILL}) "{self.lead}"'


@dataclass(frozen=True)
class ProblemFix(PatternSource):
    problem: str
    commit: str

    def cite(self) -> str:
        return f"{self.problem} ({self.commit})"


@dataclass(frozen=True)
class SeedCatalog(PatternSource):
    """A pattern's seed ID in claude-ai's numbered catalog (message 1c8729ba0ed7)."""

    seed: int

    def cite(self) -> str:
        return f"seed catalog pattern {self.seed} (claude-ai message 1c8729ba0ed7)"


@dataclass(frozen=True)
class Commit(PatternSource):
    commit: str

    def cite(self) -> str:
        return f"commit {self.commit}"


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


class BoardDisagreesPattern(BehaviourPattern):
    signature = (
        "An open order's progress reads no commit while a pull request linked to it has merged."
    )
    checks = (RuleCheck(BoardDisagrees),)
    sources = (ProblemFix("P170", "c9c0c04db"), IntentionsAsAccomplishmentsFailure())


class TacticalCommandPattern(BehaviourPattern):
    signature = (
        "The commander's own session runs an install, maintenance, connector restart or carry"
        " command instead of ordering a unit to."
    )
    checks = (CallableCheck(TacticalCommand.install_refusals),)
    sources = (ProblemFix("P171", "cba7b09c3"),)


class PlainTextPattern(BehaviourPattern):
    signature = (
        "A commander order, strike, roast or directive whose words outside cited spans match"
        " the declared jargon list in `PlainText`."
    )
    checks = (CallableCheck(PlainText),)
    sources = (CommanderRule("Hate jargon."), CeremonyFailure())


class RepeatedStrikePattern(BehaviourPattern):
    signature = (
        "A second storage strike with the same actor, owner and reason as a live strike, or a"
        " send-back strike on an order already struck since it was sent."
    )
    checks = (
        CallableCheck(StorageStrikeTodo.repeats),
        CallableCheck(Todo.struck_on_order),
    )
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
        CallableCheck(OrderCheck.parse),
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
    sources = (ProblemFix("P194", "b26843727"), CommanderRule("Strikes, then fire."))


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
