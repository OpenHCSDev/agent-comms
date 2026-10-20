"""Phase task capabilities, independent of order storage and work dispatch."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from .comms import Comms
    from .plan_phases import PlanPhase
    from .threads import Thread
    from .todos import Todo


class PhaseTask(ABC):
    instruction: ClassVar[str]

    @abstractmethod
    def start(self, comms: Comms, commander: Thread, phase: type[PlanPhase]) -> None: ...

    @abstractmethod
    def done(self, todos: Iterable[Todo]) -> bool: ...


class UnspecifiedPhaseTask(PhaseTask):
    instruction = "no checked task declared; supply its source, check and brief"

    def start(self, comms: Comms, commander: Thread, phase: type[PlanPhase]) -> None:
        """Readiness alone cannot authorize a fabricated order."""

    def done(self, todos: Iterable[Todo]) -> bool:
        return False
