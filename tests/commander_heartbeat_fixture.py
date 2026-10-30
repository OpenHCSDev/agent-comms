"""Explicit phase entries for direct consumers and the real watch publisher."""
from __future__ import annotations

from collections.abc import Mapping

from agent_comms.comms import Comms
from agent_comms.commander_heartbeat import CommanderHeartbeat, CommanderObservation
from agent_comms.commander_hud import CodexUsage, CommanderHud
from agent_comms.commander_service import CommanderService
from agent_comms.heartbeat_subjects import CommanderHistory
from agent_comms.task_attachments.commander_pings import CommanderDelivery, CommanderNotice, CommanderUpdate
from agent_comms.ui_model.unit_state import UnitState
from agent_comms.heartbeat_frame import CommanderInputs, HeartbeatFrame
from agent_comms.phase_evidence import PlanReadings
from agent_comms.threads import Thread
from agent_comms.todos import ProgressRead


def capture_frame(comms: Comms, commander: str, pulls: ProgressRead,
                  plans: PlanReadings = PlanReadings()) -> HeartbeatFrame:
    """Direct consumers acquire inputs before entering the actual frame read."""
    inputs = CommanderInputs.prepare(comms, commander, pulls, plans)
    return HeartbeatFrame.capture(comms, commander, inputs, plans)


def current_hud(comms: Comms, commander: str, frame: HeartbeatFrame,
                states: tuple[UnitState, ...]) -> CommanderHud:
    """Read explicit retained inputs for direct controls; production derives its delivery."""
    usage = CodexUsage.read()
    owner = comms.registry.require(commander).incarnation
    previous = CommanderUpdate.latest(comms, owner)
    packet = next(iter(previous), CommanderNotice(owner, ()))
    history = CommanderHistory.read(comms, CommanderService.actors(comms.registry.snapshot()))
    return CommanderHud.of(comms, commander, frame, states,
                           delivery=CommanderDelivery(packet, packet.open_pings(history)), usage=usage)


def observe_commander(comms: Comms, thread: Thread, pulls: ProgressRead,
                      plans: PlanReadings = PlanReadings()) -> CommanderObservation:
    inputs = CommanderInputs.prepare(comms, thread.name, pulls, plans)
    return CommanderObservation.read(comms, thread, inputs, plans=plans)


class ObservedHeartbeat(CommanderHeartbeat, declared_name=None):
    """Only the input is controlled; phase dispatch, writes and completion remain real."""

    def __init__(self, observed: tuple[CommanderObservation, ...]) -> None:
        super().__init__()
        self.observed = observed

    def prepare(self, comms: Comms) -> Mapping[Thread, CommanderInputs]:
        return {}

    def observe(self, comms: Comms, prepared: Mapping[Thread, CommanderInputs]) -> tuple[CommanderObservation, ...]:
        return self.observed
