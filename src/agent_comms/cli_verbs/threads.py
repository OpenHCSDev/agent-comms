from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument, WireForm
from agent_comms.presentation import ThreadRecord


@dataclass(frozen=True)
class ThreadsResult(CliResult):
    threads: tuple[Annotated[ThreadRecord, WireForm], ...]


@dataclass(frozen=True, kw_only=True)
class ThreadsCliCommand(CliCommand):
    help = "List threads"
    active_only: Annotated[bool, Argument(flags=("--active-only",))] = (
        False
    )

    def apply(self, ctx: Comms) -> ThreadsResult:
        return ThreadsResult(ctx.views.list_threads(active_only=self.active_only))
