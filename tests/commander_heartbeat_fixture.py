"""Controlled observation input through the real watch phase entry and publisher."""
from __future__ import annotations

from collections.abc import Mapping

from agent_comms.comms import Comms
from agent_comms.commander_heartbeat import CommanderHeartbeat, CommanderObservation
from agent_comms.threads import Thread
from agent_comms.todos import ProgressRead


class ObservedHeartbeat(CommanderHeartbeat, declared_name=None):
    """Only the input is controlled; phase dispatch, writes and completion remain real."""

    def __init__(self, observed: tuple[CommanderObservation, ...]) -> None:
        super().__init__()
        self.observed = observed

    def prepare(self, comms: Comms) -> Mapping[Thread, ProgressRead]:
        return {}

    def observe(self, comms: Comms, prepared: Mapping[Thread, ProgressRead]) -> tuple[CommanderObservation, ...]:
        return self.observed
