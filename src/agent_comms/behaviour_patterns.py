"""Behaviour failure patterns Core catches: each declared once with its signature and check.

A member names what the pattern looks like in state, readable without trusting
a report, the existing checks that enforce it, and where the pattern was learned.
``docs/agents/behaviour-catalog.md`` is this family's ``catalog()``; regenerate it
with ``python -m agent_comms.behaviour_patterns > docs/agents/behaviour-catalog.md``.
"""

from __future__ import annotations

import ast
from abc import ABC, abstractmethod
from dataclasses import dataclass
from inspect import isabstract
from pathlib import Path
from typing import ClassVar

from .declared_family import DeclaredFamily
from .standing_rules import BoardDisagrees, PlanIdle, StandingRule

POSTMORTEM = "https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md"
COMMANDER_SKILL = "../../src/agent_comms/skills/comms-commander/SKILL.md"


class PatternCheck(ABC):
    """An existing check that enforces a pattern."""

    @abstractmethod
    def exists(self) -> bool: ...

    @abstractmethod
    def cite(self) -> str: ...


@dataclass(frozen=True)
class RuleCheck(PatternCheck):
    """A StandingRule member that reports the pattern on every read of state."""

    rule: type[StandingRule]

    def exists(self) -> bool:
        return not isabstract(self.rule) and self.rule.declared_name is not None

    def cite(self) -> str:
        return f"standing rule `{self.rule.declared_name}` (`{self.rule.__module__}.{self.rule.__qualname__}`)"


@dataclass(frozen=True)
class RefusalCheck(PatternCheck):
    """A refusal located by its definition, read from source: the commander verbs
    open the installed bus when imported, so the catalog never imports them."""

    module: str
    qualname: str
    surface: ClassVar[str]

    def exists(self) -> bool:
        path = Path(__file__).parent.joinpath(*self.module.split(".")[1:]).with_suffix(".py")
        scope = ast.parse(path.read_text()).body
        for name in self.qualname.split("."):
            found = next((node for node in scope if isinstance(
                node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name), None)
            if found is None:
                return False
            scope = found.body
        return True

    def cite(self) -> str:
        return f"{self.surface} in `{self.module}.{self.qualname}`"


class VerbRefusal(RefusalCheck):
    surface = "verb refusal"


class ToolRefusal(RefusalCheck):
    surface = "tool refusal"


class PatternSource(ABC):
    """Where a pattern was learned."""

    @abstractmethod
    def cite(self) -> str: ...


@dataclass(frozen=True)
class PostmortemFailure(PatternSource):
    number: int

    def cite(self) -> str:
        return f"[Codex postmortem, failure {self.number}]({POSTMORTEM})"


@dataclass(frozen=True)
class CommanderRule(PatternSource):
    lead: str

    def cite(self) -> str:
        return f"[comms-commander rule]({COMMANDER_SKILL}) \"{self.lead}\""


@dataclass(frozen=True)
class ProblemOrder(PatternSource):
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
        return (f"## {cls.declared_name}\n\n{cls.signature}\n\n"
                f"Checked by:\n\n{checks}\nLearned from: {sources}\n")

    @classmethod
    def catalog(cls) -> str:
        members = cls.members_with(BehaviourPattern)
        cited = {failure: [m.declared_name for m in members if PostmortemFailure(failure) in m.sources]
                 for failure in range(1, 11)}
        failures = "".join(
            f"- [Failure {failure}]({POSTMORTEM}): "
            + (", ".join(f"`{name}`" for name in names) if names else "no pattern checks it yet")
            + "\n" for failure, names in cited.items())
        return (
            "# Behaviour catalog\n\n"
            "Generated from `agent_comms.behaviour_patterns` by\n"
            "`python -m agent_comms.behaviour_patterns > docs/agents/behaviour-catalog.md`;"
            " do not edit by hand.\n\n"
            "Each behaviour failure pattern is declared once in Core with its signature (what it looks"
            " like in state, readable without trusting a report) and the existing checks that enforce"
            " it. Patterns in code, rather than in behaviour, are the refactor-audit skill's catalog"
            " (`refactor-audit/references/patterns/README.md`).\n\n"
            f"The [Codex postmortem]({POSTMORTEM}) names ten failures; each pattern cites the ones it"
            " checks:\n\n"
            f"{failures}\n"
            + "\n".join(member.section() for member in members))


class PlanIdlePattern(BehaviourPattern):
    signature = ("A plan phase whose earlier phases are all done is not done itself, and no open"
                 " order held by a unit serves it.")
    checks = (RuleCheck(PlanIdle),)
    sources = (ProblemOrder("P83", "34ba68c81"),)


class BoardDisagreesPattern(BehaviourPattern):
    signature = "An open order's progress reads no commit while a pull request linked to it has merged."
    checks = (RuleCheck(BoardDisagrees),)
    sources = (ProblemOrder("P170", "c9c0c04db"), PostmortemFailure(7))


class TacticalCommandPattern(BehaviourPattern):
    signature = ("The commander's own session runs an install, maintenance, connector restart or carry"
                 " command instead of ordering a unit to.")
    checks = (ToolRefusal("agent_comms.commander.send_guard", "TacticalCommand"),)
    sources = (ProblemOrder("P171", "cba7b09c3"),)


class PlainTextPattern(BehaviourPattern):
    signature = ("A commander order, strike, roast or directive whose words outside cited spans match"
                 " the declared jargon list in `PlainText`.")
    checks = (VerbRefusal("agent_comms.commander.order", "refuse"),
              ToolRefusal("agent_comms.connector", "DirectorCaller.require_directive"))
    sources = (CommanderRule("Hate jargon."), PostmortemFailure(8))


class UndeclaredAudiencePattern(BehaviourPattern):
    signature = "A send to a channel, which reaches many threads, names no thread as its audience."
    checks = (ToolRefusal("agent_comms.channel_targets", "ChannelTarget.addressed"),)
    sources = (Commit("dc4fb3bfa"), PostmortemFailure(8))


class RepeatedStrikePattern(BehaviourPattern):
    signature = ("A second strike for one miss: the same actor, owner and reason as a live strike, or a"
                 " send-back strike on an order already struck since it was sent.")
    checks = (ToolRefusal("agent_comms.todos", "StrikeTodo.repeats"),
              VerbRefusal("agent_comms.commander.order", "send"))
    sources = (ProblemOrder("P184", "7af14a520"),)


class StrikeInErrorPattern(BehaviourPattern):
    signature = ("A strike recorded in error is deleted, or voided while not live, instead of a void"
                 " recorded beside it with a plain reason.")
    checks = (VerbRefusal("agent_comms.commander.order", "unstrike"),
              ToolRefusal("agent_comms.todos", "VoidStrikeTodo.apply"))
    sources = (ProblemOrder("P47", "15d435152"),)


class UncountedCheckPattern(BehaviourPattern):
    signature = ("An order whose check line does not read `repo :: command -> integer`, prints no"
                 " integer, or already prints its target before any work.")
    checks = (VerbRefusal("agent_comms.goals", "OrderCheck.parse"),
              VerbRefusal("agent_comms.goals", "NoIntegerPrinted"),
              VerbRefusal("agent_comms.commander.order", "correct_check"))
    sources = (ProblemOrder("P114", "d889df612"), PostmortemFailure(4), PostmortemFailure(7))


class StrikeOnNamedProblemPattern(BehaviourPattern):
    signature = ("A strike or hunt recorded on a problem other than the one given as its argument, such"
                 " as one moved by a problem number inside its reason.")
    checks = (VerbRefusal("agent_comms.commander.order", "strike"),
              VerbRefusal("agent_comms.commander.order", "hunt"))
    sources = (ProblemOrder("P194", "b26843727"), CommanderRule("Strikes, then fire."))


if __name__ == "__main__":
    print(BehaviourPattern.catalog(), end="")
