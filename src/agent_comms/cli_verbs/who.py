from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import WireForm
from agent_comms.presentation import ThreadView


@dataclass(frozen=True)
class WhoResult(CliResult):
    who: tuple[Annotated[ThreadView, WireForm], ...]


@dataclass(frozen=True, kw_only=True)
class WhoCliCommand(CliCommand):
    help = "Presence: who is in the chat"

    def apply(self, ctx: Comms) -> WhoResult:
        return WhoResult(ctx.views.who())
