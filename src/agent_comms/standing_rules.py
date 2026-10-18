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

if TYPE_CHECKING:
    from .comms import Comms
    from .todos import Todo


class StandingRule(DeclaredFamily, affix="StandingRule", discovery_package="agent_comms.commander"):
    @classmethod
    @abstractmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]: ...

    @abstractmethod
    def line(self) -> str: ...

    @staticmethod
    def todos(comms: Comms, commander: str) -> tuple[Todo, ...]:
        return tuple(comms.goals.todos.for_commander(comms.registry.require(commander).incarnation))

    @classmethod
    def broken(cls, comms: Comms, commander: str) -> tuple[StandingRule, ...]:
        """Every member's current violations for one commander."""
        return tuple(
            found for rule in cls.members_with(cls) for found in rule.violations(comms, commander)
        )


@dataclass(frozen=True)
class ReasonedStandingRule(StandingRule, declared_name=None):
    """A rule broken at most once, saying why in its declared line."""

    why: str
    text: ClassVar[str]

    @classmethod
    @abstractmethod
    def reason(cls, comms: Comms, commander: str) -> str | None: ...

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return tuple(cls(why) for why in (cls.reason(comms, commander),) if why)

    def line(self) -> str:
        return self.text.format(why=self.why)


@dataclass(frozen=True)
class HuntDue(ReasonedStandingRule):
    seconds: ClassVar[int] = 900
    text = "HUNT DUE: read a hot path and order the fix this wake"

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        hunted = (hunt.at.at for todo in cls.todos(comms, commander) for hunt in todo.hunts)
        age = time.time() - max(hunted, default=0)
        return f"last hunt {int(age // 60)} min ago" if age > cls.seconds else None


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
class PlanIdle(StandingRule):
    """A phase whose dependencies are done stalls unnoticed unless some order serves it."""

    phase: type[PlanPhase]

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        todos = cls.todos(comms, commander)
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
