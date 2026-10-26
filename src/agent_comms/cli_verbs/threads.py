from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import ThreadsResult
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class ThreadsCliCommand(CliCommand):
    help = "List threads"
    active_only: Annotated[bool, Argument(flags=("--active-only",))] = (
        False
    )

    def apply(self, ctx: Comms) -> ThreadsResult:
        return ThreadsResult(ctx.views.list_threads(active_only=self.active_only))
