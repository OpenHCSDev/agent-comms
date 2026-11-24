"""Phase task capabilities, independent of order storage and work dispatch."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from agent_comms.planning.order_phase import OrderPhase
from agent_comms.planning.plan_reading import PlanReading
from agent_comms.registry.threads import Thread


class PhaseTask[Context, Phase: OrderPhase](ABC):
    instruction: ClassVar[str]

    @abstractmethod
    def start(
        self, comms: Context, assigner: Thread, phase: type[Phase], reading: PlanReading
    ) -> None: ...


class UnspecifiedPhaseTask(PhaseTask[object, OrderPhase]):
    instruction = "no checked task declared; supply its source, check and brief"

    def start(
        self, comms: object, assigner: Thread, phase: type[OrderPhase], reading: PlanReading
    ) -> None:
        """Readiness alone cannot authorize a fabricated order."""
