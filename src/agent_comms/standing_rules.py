"""Standing rules the commander keeps: each member derives its violations on read."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from .declared_family import DeclaredFamily
from .plan_phases import PlanPhase

if TYPE_CHECKING:
    from .todos import Todo


class StandingRule(DeclaredFamily, affix="StandingRule"):
    @classmethod
    @abstractmethod
    def violations(cls, todos: Sequence[Todo]) -> tuple[Self, ...]: ...

    @abstractmethod
    def line(self) -> str: ...

    @classmethod
    def broken(cls, todos: Sequence[Todo]) -> tuple[StandingRule, ...]:
        """Every member's current violations, read from the commander's Todos."""
        return tuple(found for rule in cls.members_with(cls) for found in rule.violations(todos))


@dataclass(frozen=True)
class PlanIdle(StandingRule):
    """A phase whose dependencies are done stalls unnoticed unless some order serves it."""

    phase: type[PlanPhase]

    @classmethod
    def violations(cls, todos: Sequence[Todo]) -> tuple[Self, ...]:
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
