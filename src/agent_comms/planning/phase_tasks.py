"""Phase task capabilities, independent of order storage and work dispatch."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from agent_comms.comms import Comms
from agent_comms.planning.plan_reading import PlanReading
from agent_comms.registry.threads import Thread

if TYPE_CHECKING:
    from agent_comms.planning.plan_phase_base import PlanPhase


class PhaseTask(ABC):
    instruction: ClassVar[str]

    @abstractmethod
    def start(
        self, comms: Comms, assigner: Thread, phase: type[PlanPhase], reading: PlanReading
    ) -> None: ...


class UnspecifiedPhaseTask(PhaseTask):
    instruction = "no checked task declared; supply its source, check and brief"

    def start(
        self, comms: Comms, assigner: Thread, phase: type[PlanPhase], reading: PlanReading
    ) -> None:
        """Readiness alone cannot authorize a fabricated order."""
