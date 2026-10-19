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

from .channel_targets import ChannelTarget
from .commander import order
from .commander.send_guard import TacticalCommand
from .connector import DirectorCaller
from .declared_family import DeclaredFamily
from .goals import NoIntegerPrinted, OrderCheck
from .standing_rules import BoardDisagrees, PlanIdle, StandingRule
from .todos import StrikeTodo, VoidStrikeTodo

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
class RefusalCheck(PatternCheck):
    """A refusal, held as the function or class that refuses."""

    refusal: Callable[..., object]
    surface: ClassVar[str]

    def cite(self) -> str:
        return f"{self.surface} in `{self.refusal.__module__}.{self.refusal.__qualname__}`"


class VerbRefusal(RefusalCheck):
    surface = "verb refusal"


class ToolRefusal(RefusalCheck):
    surface = "tool refusal"


class PatternSource(ABC):
    """Where a pattern was learned."""

    @abstractmethod
    def cite(self) -> str: ...


class PostmortemFailure(PatternSource, DeclaredFamily, affix="Failure"):
    """A failure the Codex postmortem names; a pattern that checks it cites an instance."""

    number: ClassVar[int]
    title: ClassVar[str]

    def cite(self) -> str:
        return f"[Codex postmortem, failure {self.number}]({POSTMORTEM})"


class RewroteFrameworkFailure(PostmortemFailure):
    number = 1
    title = "It rewrote the framework instead of questioning its own design"


class AddedStatesFailure(PostmortemFailure):
    number = 2
    title = "It added states instead of removing them"


class ViolatedPrincipleFailure(PostmortemFailure):
    number = 3
    title = "It wrote down the right principle and then violated it"


class MeasuredWrongThingFailure(PostmortemFailure):
    number = 4
    title = "It measured the wrong thing and reported it as progress"


class RedefinedRealAppFailure(PostmortemFailure):
    number = 5
    title = 'It redefined "the real app" until something passed'


class IgnoredOrdersFailure(PostmortemFailure):
    number = 6
    title = "It ignored direct orders"


class IntentionsAsAccomplishmentsFailure(PostmortemFailure):
    number = 7
    title = "It described its intentions as its accomplishments"


class CeremonyFailure(PostmortemFailure):
    number = 8
    title = "It drowned the work in ceremony"


class MergedOverChecksFailure(PostmortemFailure):
    number = 9
    title = "It merged over its own checks and broke things in circles"


class InventedRulesFailure(PostmortemFailure):
    number = 10
    title = "It invented rules and blamed them on Tristan"


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
        cited = {
            failure: [
                m.declared_name for m in members if any(isinstance(s, failure) for s in m.sources)
            ]
            for failure in PostmortemFailure.members_with(PostmortemFailure)
        }
        failures = "".join(
            f"- [Failure {failure.number}]({POSTMORTEM}), {failure.title}: "
            + (", ".join(f"`{name}`" for name in names) if names else "no pattern checks it yet")
            + "\n"
            for failure, names in cited.items()
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
            f"The [Codex postmortem]({POSTMORTEM}) names these failures; each pattern cites the"
            " ones it checks:\n\n"
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
    checks = (ToolRefusal(TacticalCommand),)
    sources = (ProblemFix("P171", "cba7b09c3"),)


class PlainTextPattern(BehaviourPattern):
    signature = (
        "A commander order, strike, roast or directive whose words outside cited spans match"
        " the declared jargon list in `PlainText`."
    )
    checks = (
        VerbRefusal(order.refuse),
        ToolRefusal(DirectorCaller.require_directive),
    )
    sources = (CommanderRule("Hate jargon."), CeremonyFailure())


class UndeclaredAudiencePattern(BehaviourPattern):
    signature = "A send to a channel, which reaches many threads, names no thread as its audience."
    checks = (ToolRefusal(ChannelTarget.addressed),)
    sources = (Commit("dc4fb3bfa"), CeremonyFailure())


class RepeatedStrikePattern(BehaviourPattern):
    signature = (
        "A second strike for one miss: the same actor, owner and reason as a live strike, or a"
        " send-back strike on an order already struck since it was sent."
    )
    checks = (
        ToolRefusal(StrikeTodo.repeats),
        VerbRefusal(order.send),
    )
    sources = (ProblemFix("P184", "7af14a520"),)


class StrikeInErrorPattern(BehaviourPattern):
    signature = (
        "A strike recorded in error is deleted, or voided while not live, instead of a void"
        " recorded beside it with a plain reason."
    )
    checks = (
        VerbRefusal(order.unstrike),
        ToolRefusal(VoidStrikeTodo.apply),
    )
    sources = (ProblemFix("P47", "15d435152"),)


class UncountedCheckPattern(BehaviourPattern):
    signature = (
        "An order whose check line does not read `repo :: command -> integer`, prints no"
        " integer, or already prints its target before any work."
    )
    checks = (
        VerbRefusal(OrderCheck.parse),
        VerbRefusal(NoIntegerPrinted),
        VerbRefusal(order.correct_check),
    )
    sources = (
        ProblemFix("P114", "d889df612"),
        MeasuredWrongThingFailure(),
        IntentionsAsAccomplishmentsFailure(),
    )


class StrikeOnNamedProblemPattern(BehaviourPattern):
    signature = (
        "A strike or hunt recorded on a problem other than the one given as its argument, such"
        " as one moved by a problem number inside its reason."
    )
    checks = (
        VerbRefusal(order.strike),
        VerbRefusal(order.hunt),
    )
    sources = (ProblemFix("P194", "b26843727"), CommanderRule("Strikes, then fire."))


if __name__ == "__main__":
    print(BehaviourPattern.catalog(), end="")
