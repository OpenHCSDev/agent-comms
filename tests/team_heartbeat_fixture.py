"""Explicit phase entries for direct consumers and the real watch publisher."""
from __future__ import annotations

from agent_comms.comms import Comms
from agent_comms.heartbeat_subjects import TeamHistory
from agent_comms.planning.nominal_census import NominalPlanReadings
from agent_comms.planning.orders import ProgressRead
from agent_comms.planning.plan_reading import PlanReadings, RetainedPlanReadings
from agent_comms.task_attachments.team_pings import TeamDelivery, TeamUpdate, TeamUpdateNotice
from agent_comms.team.standing_rules import (
    HeartbeatFrame,
    TeamInputs,
)
from agent_comms.team.team_heartbeat import TeamHeartbeat, TeamObservation
from agent_comms.team.team_hud import CodexUsage, TeamHud
from agent_comms.team.team_service import TeamService
from agent_comms.ui_model.teammate_state import TeammateState


class RetainedNominalPlanReadings(RetainedPlanReadings, NominalPlanReadings):
    """The live heartbeat's plan reading: answers a main commit still supports carry forward,
    so a frame re-reads only the evidence its commit changed."""


LIVE_PLAN_READINGS = RetainedNominalPlanReadings()


def capture_frame(comms: Comms, pulls: ProgressRead,
                  plans: PlanReadings = LIVE_PLAN_READINGS) -> HeartbeatFrame:
    """Direct consumers acquire inputs before entering the actual frame read."""
    inputs = TeamInputs.prepare(comms, comms.registry.snapshot().team_project.require(), pulls, plans)
    frame = HeartbeatFrame.capture(comms, inputs, plans)
    plans.finish_pass()
    return frame


def current_hud(comms: Comms, reader: str, frame: HeartbeatFrame,
                states: tuple[TeammateState, ...], usage: CodexUsage) -> TeamHud:
    """The team board as one teammate reads it; production derives its delivery.

    `usage` is the Codex account reading the board shows. A test comparing with a published
    board passes the reading that board published: the live account answers a second read
    with whatever its reset clock says by then."""
    owner = comms.registry.require(reader).incarnation
    previous = TeamUpdate.latest(comms, owner)
    packet = next(iter(previous), TeamUpdateNotice(owner, ()))
    history = TeamHistory.read(comms, TeamService.actors(comms.registry.snapshot()))
    return TeamHud.of(comms, frame, states,
                           delivery=TeamDelivery(packet, packet.open_pings(history)), usage=usage)


def observe_team(comms: Comms, pulls: ProgressRead,
                 plans: PlanReadings = LIVE_PLAN_READINGS) -> TeamObservation:
    inputs = TeamInputs.prepare(comms, comms.registry.snapshot().team_project.require(), pulls, plans)
    observation = TeamObservation.read(comms, inputs, plans=plans)
    plans.finish_pass()
    return observation


class ObservedHeartbeat(TeamHeartbeat, declared_name=None):
    """Only the input is controlled; phase dispatch, writes and completion remain real."""

    def __init__(self, *observed: TeamObservation) -> None:
        """The pass's boards: one team board, or none for a bus without one."""
        super().__init__()
        self.observed = observed

    def prepare(self, comms: Comms) -> tuple[TeamInputs, ...]:
        return ()

    def observe(self, comms: Comms, prepared: tuple[TeamInputs, ...]) -> tuple[TeamObservation, ...]:
        return self.observed
