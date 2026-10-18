"""Standing rules the commander keeps: each member derives its violations on read."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from .declared_family import DeclaredFamily
from .plan_phases import PlanPhase

if TYPE_CHECKING:
    from .registration import Registration
    from .storage_rules import StorageRule
    from .todos import Todo


class StandingRule(DeclaredFamily, affix="StandingRule"):
    @classmethod
    @abstractmethod
    def violations(cls, todos: Sequence[Todo], registry: Registration) -> tuple[Self, ...]: ...

    @abstractmethod
    def line(self) -> str: ...

    @classmethod
    def broken(cls, todos: Sequence[Todo], registry: Registration) -> tuple[StandingRule, ...]:
        """Every member's current violations, read from the commander's Todos and the registry."""
        return tuple(
            found for rule in cls.members_with(cls) for found in rule.violations(todos, registry)
        )


@dataclass(frozen=True)
class PlanIdle(StandingRule):
    """A phase whose dependencies are done stalls unnoticed unless some order serves it."""

    phase: type[PlanPhase]

    @classmethod
    def violations(cls, todos: Sequence[Todo], registry: Registration) -> tuple[Self, ...]:
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
    def violations(cls, todos: Sequence[Todo], registry: Registration) -> tuple[Self, ...]:
        rule = registry.storage_rule
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
