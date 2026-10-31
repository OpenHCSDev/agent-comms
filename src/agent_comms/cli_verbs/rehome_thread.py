from __future__ import annotations

from agent_comms.cli_result import CliResult
from agent_comms.cli_support.conversation_limits import ConversationLimitsCliCommand
from agent_comms.cli_support.running_session import RunningSessionCliCommand
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class RehomeThreadCliCommand(
    RunningSessionCliCommand, ConversationLimitsCliCommand, declared_name="rehome-thread",
):
    help = ("Move an existing unit from its harness onto native Pi, keeping its birth, goal, "
            "orders and inbox; retires the harness's bridge for that session")
    name: Annotated[
        str,
        Argument(flags=("--name",), options={"help": "The existing agent-comms thread to move"}),
    ]

    def apply(self, ctx: Comms) -> CliResult:
        worktree = ctx.registry.require(self.name).worktree
        return ctx.threads.rehome_thread(
            self.history(Path(worktree)), name=self.name, limits=self.limits)
