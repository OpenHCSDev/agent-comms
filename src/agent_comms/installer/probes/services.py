from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import PathText
from agent_comms.host_services import HostService
from agent_comms.installer.interpreter_operation import RuntimeProbe, TargetProbe
from agent_comms.service_passes import ServicePassStore


class ServingProbe(RuntimeProbe[None]):
    """Every running host service serves the root, proven by the runtime that owns it."""

    def run(self) -> None:
        for member in HostService.members_with(HostService):
            for instance in member.running():
                instance.serves()


@dataclass(frozen=True)
class CommanderHeartbeatProbe(TargetProbe, RuntimeProbe[None]):
    """Only the target runtime interprets its post-restart heartbeat evidence."""

    root: Annotated[Path, PathText]
    since: float

    def run(self) -> None:
        comms = wire(self.root)
        # The heartbeat's own served set: a retired or dead #commanders member gets no pass.
        commanders = tuple(thread.incarnation.require_recorded()
                           for thread in CommanderHeartbeat.served(comms.registry, comms.goals))
        if not commanders:
            print("Commander heartbeat: no live served commander; not qualified")
            return
        asyncio.run(ServicePassStore.at(self.root).wait_completed(
            commanders, self.since, CommanderHeartbeat,
        ))
        current = comms.registry.snapshot()
        if not all(owner.current(current) for owner in commanders):
            raise RelationViolationError("Commander changed during post-restart heartbeat qualification")
        print("Commander heartbeat: ordinary post-restart completion for "
              + ", ".join(owner.name for owner in commanders))
