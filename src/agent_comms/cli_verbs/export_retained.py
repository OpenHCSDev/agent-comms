from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument


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
