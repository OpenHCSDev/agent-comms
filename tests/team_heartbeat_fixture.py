"""Explicit phase entries for direct consumers and the real watch publisher."""
from __future__ import annotations

from collections.abc import Mapping

from agent_comms.comms import Comms
from agent_comms.team_heartbeat import TeamHeartbeat, TeamObservation
from agent_comms.team_hud import CodexUsage, TeamHud
from agent_comms.team_service import TeamService
from agent_comms.heartbeat_subjects import TeamHistory
from agent_comms.task_attachments.team_pings import TeamDelivery, TeamUpdateNotice, TeamUpdate
from agent_comms.ui_model.teammate_state import TeammateState
from agent_comms.heartbeat_frame import TeamInputs, HeartbeatFrame
from agent_comms.nominal_census import NominalPlanReadings
from agent_comms.plan_reading import PlanReadings, RetainedPlanReadings
from agent_comms.threads import Thread
from agent_comms.todos import ProgressRead


class RetainedNominalPlanReadings(RetainedPlanReadings, NominalPlanReadings):
    """The live heartbeat's plan reading: answers a main commit still supports carry forward,
    so a frame re-reads only the evidence its commit changed."""


LIVE_PLAN_READINGS = RetainedNominalPlanReadings()


def capture_frame(comms: Comms, lead: str, pulls: ProgressRead,
                  plans: PlanReadings = LIVE_PLAN_READINGS) -> HeartbeatFrame:
    """Direct consumers acquire inputs before entering the actual frame read."""
    inputs = TeamInputs.prepare(comms, lead, pulls, plans)
    return HeartbeatFrame.capture(comms, lead, inputs, plans)


def current_hud(comms: Comms, lead: str, frame: HeartbeatFrame,
                states: tuple[TeammateState, ...]) -> TeamHud:
    """Read explicit retained inputs for direct controls; production derives its delivery."""
    usage = CodexUsage.read()
    owner = comms.registry.require(lead).incarnation
    previous = TeamUpdate.latest(comms, owner)
    packet = next(iter(previous), TeamUpdateNotice(owner, ()))
    history = TeamHistory.read(comms, TeamService.actors(comms.registry.snapshot()))
    return TeamHud.of(comms, lead, frame, states,
                           delivery=TeamDelivery(packet, packet.open_pings(history)), usage=usage)


def observe_team(comms: Comms, thread: Thread, pulls: ProgressRead,
                 plans: PlanReadings = LIVE_PLAN_READINGS) -> TeamObservation:
    inputs = TeamInputs.prepare(comms, thread.name, pulls, plans)
    return TeamObservation.read(comms, thread, inputs, plans=plans)


class ObservedHeartbeat(TeamHeartbeat, declared_name=None):
    """Only the input is controlled; phase dispatch, writes and completion remain real."""

    def __init__(self, observed: tuple[TeamObservation, ...]) -> None:
        super().__init__()
        self.observed = observed

    def prepare(self, comms: Comms) -> Mapping[Thread, TeamInputs]:
        return {}

    def observe(self, comms: Comms, prepared: Mapping[Thread, TeamInputs]) -> tuple[TeamObservation, ...]:
        return self.observed
