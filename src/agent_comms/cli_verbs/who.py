from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import WhoResult
from agent_comms.comms import Comms
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class WhoCliCommand(CliCommand):
    help = "Presence: who is in the chat"

    def apply(self, ctx: Comms) -> WhoResult:
        return WhoResult(ctx.views.who())
