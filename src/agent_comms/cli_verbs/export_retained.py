"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class ExportRetainedCliCommand(CliCommand, declared_name="export-retained"):
    help = "Export current authored context from original checked sources"
    thread: Annotated[str, Argument(flags=("thread",))]
    output: Annotated[str, Argument(flags=("--output",))]
    overwrite: Annotated[bool, Argument(flags=("--overwrite",))] = (
        False
    )

    def apply(self, ctx: Comms) -> CliResult:
        return ctx.views.retained_context(self.thread).export(
            self.output, overwrite=self.overwrite)
