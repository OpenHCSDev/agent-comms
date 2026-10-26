from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import AcknowledgedResult
from agent_comms.cli_commands import ThreadArgument
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class AckCliCommand(ThreadArgument, CliCommand):
    help = "Mark inbox delivered"

    def apply(self, ctx: Comms) -> CliResult:
        messages = ctx.bus.inbox(self.thread).page.messages
        return AcknowledgedResult(ctx.messaging.acknowledge(
            self.thread, tuple(message.reference for message in messages)))
