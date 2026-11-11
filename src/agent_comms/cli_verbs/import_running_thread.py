from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_support.importing import ImportingCliCommand
from agent_comms.cli_support.running_session import RunningSessionCliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument


@dataclass(frozen=True, kw_only=True)
class ImportRunningThreadCliCommand(
    RunningSessionCliCommand, ImportingCliCommand, declared_name="import-running-thread",
):
    help = "Import a session a running Claude Code, Codex or OpenCode holds, as a stopped thread"
    name: Annotated[
        str,
        Argument(flags=("--name",), options={"help": "New agent-comms thread name"}),
    ]

    def apply(self, ctx: Comms) -> CliResult:
        return self.imported(ctx, self.history(self.worktree.local_directory()), self.name)
