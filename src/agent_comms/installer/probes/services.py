from __future__ import annotations

from agent_comms.installer.interpreter_operation import RuntimeProbe


class ServingProbe(RuntimeProbe[None]):
    """Every running host service serves the root, proven by the runtime that owns it."""

    def run(self) -> None:
        from agent_comms.host_services import HostService

        for member in HostService.members_with(HostService):
            for instance in member.running():
                instance.serves()

