from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar

from agent_comms.channel_targets import CommsTag
from agent_comms.comms import client_wire
from agent_comms.field_codec import PathText
from agent_comms.installer.interpreter_operation import OutgoingProbe, RuntimeProbe
from agent_comms.service_sender import ServiceSender


@dataclass(frozen=True)
class InstallReportProbe(RuntimeProbe[None], ServiceSender, OutgoingProbe):
    """Publish using the selected runtime's own registry, schema and publisher."""

    sender: ClassVar[str] = "installer"
    root: Annotated[Path, PathText]
    body: str

    def run(self) -> None:
        with client_wire(self.root) as comms:
            self.register(comms.registry)
            comms.messaging.send(self.sender, CommsTag.channel(), self.body)
