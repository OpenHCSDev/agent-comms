"""What one heartbeat reads from GitHub and the plans, read once and shared by every rule."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from agent_comms.phase_evidence import MainTree, PlanReading
from agent_comms.plan_phases import PlanPhase
from agent_comms.repo import Repo
from agent_comms.standing_rules import OwedAnswer, StandingScan

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.todos import ProgressRead


@dataclass(frozen=True)
class HeartbeatFrame(StandingScan):
    """One beat's forge and plan facts. Every standing rule reads them from here, so a rule that
    builds its own reader is a second copy of what GitHub said this beat."""

    pulls: ProgressRead
    plan: PlanReading
    answers: tuple[OwedAnswer, ...]

    @classmethod
    def read(cls, comms: Comms, commander: str, pulls: ProgressRead) -> Self:
        scan = StandingScan.read(comms, commander)
        snapshot = comms.registry.snapshot()
        repository = Repo.declaring(
            snapshot.commander_project.require().repos, PlanPhase.__module__
        )
        return cls(
            scan.todos,
            pulls.register(scan.todos),
            PlanReading(
                comms.goals.todos.history_for_commander(snapshot.require(commander).incarnation),
                MainTree.of(repository),
            ),
            OwedAnswer.read_scan(comms, commander, scan),
        )
