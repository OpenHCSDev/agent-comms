"""Phase task capabilities, independent of order storage and work dispatch."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from .comms import Comms
    from .phase_evidence import PlanReading
    from .plan_phases import PlanPhase
    from .threads import Thread


class PhaseTask(ABC):
    instruction: ClassVar[str]

    @abstractmethod
    def start(
        self, comms: Comms, commander: Thread, phase: type[PlanPhase], reading: PlanReading
    ) -> None: ...


class UnspecifiedPhaseTask(PhaseTask):
    instruction = "no checked task declared; supply its source, check and brief"

    def start(
        self, comms: Comms, commander: Thread, phase: type[PlanPhase], reading: PlanReading
    ) -> None:
        """Readiness alone cannot authorize a fabricated order."""
