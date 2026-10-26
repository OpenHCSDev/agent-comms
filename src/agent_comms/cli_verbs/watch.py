from __future__ import annotations

from agent_comms.active_route import CommsRoute
from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import ThreadArgument
from agent_comms.cli_commands import WatchStream
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.field_codec import Editor
from agent_comms.message_reference import FirstUnreadStart
from agent_comms.message_reference import InboxStart
from agent_comms.read_basis import OnReadWatchAcknowledgment
from agent_comms.read_basis import WatchAcknowledgment
from agent_comms.target_fields import ChoiceTargetField
from dataclasses import dataclass
from typing import Annotated
from typing import TextIO
import json


@dataclass(frozen=True, kw_only=True)
class WatchCliCommand(ThreadArgument, CliCommand[WatchStream], declared_name="watch"):
    help = "Stream unread and arriving messages as JSON lines"
    after: Annotated[
        InboxStart,
        Argument(
            flags=("--after",),
            options={
                "type": json.loads,
                "help": "Exclusive original message reference already attempted by this receiver (JSON)",
            },
        ),
    ] = FirstUnreadStart()
    acknowledgment: Annotated[
        type[WatchAcknowledgment],
        Editor(ChoiceTargetField),
        Argument(flags=("--acknowledgment",)),
    ] = OnReadWatchAcknowledgment

    def apply(self, ctx: Comms) -> WatchStream:
        return WatchStream(ctx, self)

    def execute(self, route: CommsRoute, output: TextIO) -> int:
        with Comms.client(route) as comms:
            stream = self.apply(comms)
        return stream.write(output)
