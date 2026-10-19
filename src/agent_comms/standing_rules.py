"""Standing rules the commander keeps: each member derives its violations on read.

Members reading Core state are declared here; members reading the commander's own
files are declared in ``agent_comms.commander.standing`` until those files move into Core.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from .declared_family import DeclaredFamily
from .plan_phases import PlanPhase
from .rule_reviews import RuleReviews

if TYPE_CHECKING:
    from .comms import Comms
    from .storage_rules import StorageRule
    from .todos import Todo


class StandingRule(DeclaredFamily, affix="StandingRule", discovery_package="agent_comms.commander"):
    @classmethod
    @abstractmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]: ...

    @abstractmethod
    def line(self) -> str: ...

    def began(self) -> float | None:
        """When this violation began, or None when its rule cannot say: it shows on every read."""
        return None

    @staticmethod
    def todos(comms: Comms, commander: str) -> tuple[Todo, ...]:
        return comms.goals.todos.held_for_commander(comms.registry.require(commander).incarnation)

    @classmethod
    def broken(cls, comms: Comms, commander: str, after: float = 0.0) -> tuple[StandingRule, ...]:
        """Every member's violations for one commander that began after ``after``."""
        found = (v for rule in cls.members_with(cls) for v in rule.violations(comms, commander))
        return tuple(rule for rule in found if (began := rule.began()) is None or began >= after)


@dataclass(frozen=True)
class ReasonedStandingRule(StandingRule, declared_name=None):
    """A rule broken at most once, saying why in its declared line."""

    why: str
    since: float | None = None
    text: ClassVar[str]

    @classmethod
    @abstractmethod
    def reason(cls, comms: Comms, commander: str) -> str | None: ...

    @classmethod
    def broken_at(cls, comms: Comms, commander: str) -> float | None:
        return None

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        why = cls.reason(comms, commander)
        return (cls(why, cls.broken_at(comms, commander)),) if why else ()

    def began(self) -> float | None:
        return self.since

    def line(self) -> str:
        return self.text.format(why=self.why)


@dataclass(frozen=True)
class HuntDue(ReasonedStandingRule):
    seconds: ClassVar[int] = 900
    text = "HUNT DUE: read a hot path and order the fix this wake"

    @classmethod
    def hunted(cls, comms: Comms, commander: str) -> float:
        hunts = (
            hunt
            for todo in comms.goals.todos.history_for_commander(
                comms.registry.require(commander).incarnation
            )
            for hunt in todo.hunts
        )
        return max((hunt.at.recorded_at("A hunt") for hunt in hunts), default=0)

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        age = time.time() - cls.hunted(comms, commander)
        return f"last hunt {int(age // 60)} min ago" if age > cls.seconds else None

    @classmethod
    def broken_at(cls, comms: Comms, commander: str) -> float | None:
        return cls.hunted(comms, commander) + cls.seconds


@dataclass(frozen=True)
class RulesDue(StandingRule):
    """A reviewer acts only while its last review is fresh and every drawn rule is answered."""

    max_age: ClassVar[int] = 20 * 60
    why: str
    since: float

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return RuleReviews(comms.root).review(commander).due(cls, commander)

    def began(self) -> float:
        return self.since

    def line(self) -> str:
        return (
            f"RULES DUE ({self.why}) -> agent-comms-commander rules review"
            " and answer the drawn rules before any order"
        )


@dataclass(frozen=True)
class BridgeDown(ReasonedStandingRule):
    text = (
        "COMMS BRIDGE DOWN: the commander receives nothing -> re-arm the bridge Monitor this wake"
    )

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        receiving = comms.registry.snapshot().statuses[commander].receiving
        return None if receiving else f"{commander} is not receiving"


@dataclass(frozen=True)
class BoardDisagrees(StandingRule):
    """An open order reads as no commit despite its linked pull request having merged."""

    problem: str
    order: str
    merge: str

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return tuple(
            cls(todo.id, todo.order.id, merged.sha)
            for todo, merged in comms.goals.todos.board_disagreements(
                comms.registry.require(commander).incarnation, comms.registry.storage_rule
            )
        )

    def line(self) -> str:
        return (
            f"BOARD DISAGREES: {self.problem} order {self.order} reads no commit, "
            f"but its linked PR merged as {self.merge} -> repair the progress read this wake"
        )


@dataclass(frozen=True)
class PlanIdle(StandingRule):
    """A phase whose dependencies are done stalls unnoticed unless some order serves it."""

    phase: type[PlanPhase]

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        todos = comms.goals.todos.history_for_commander(
            comms.registry.require(commander).incarnation
        )
        return tuple(
            cls(phase)
            for phase in PlanPhase.members_with(PlanPhase)
            if phase.ready(todos) and not phase.ordered(todos)
        )

    def line(self) -> str:
        return (
            f"PLAN IDLE: phase {self.phase.declared_name} ready, no open order"
            " -> dispatch it this wake"
        )


@dataclass(frozen=True)
class DiskLow(StandingRule):
    """Under the cleanup threshold a cleanup owner is due; under the floor nothing allocates."""

    rule: StorageRule
    free_gib: float

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        rule = comms.registry.storage_rule
        free = rule.free_gib()
        return (cls(rule, free),) if rule.cleanup_due(free) else ()

    def line(self) -> str:
        floor = (
            f"; under the {self.rule.floor_gib:g} GiB floor nothing allocates"
            if self.rule.below_floor(self.free_gib)
            else ""
        )
        return (
            f"DISK LOW: {self.rule.volume} has {self.free_gib:.1f} GiB free (cleanup due under"
            f" {self.rule.cleanup_gib:g} GiB{floor}) -> put a cleanup owner on it now"
        )
