"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import AppliedTogether
from agent_comms.cli_commands import ThreadStoppedResult
from agent_comms.cli_verbs.thread_lifecycle import ThreadLifecycleCliCommand
from agent_comms.comms import Comms
from agent_comms.thread_execution import NativeOwnerCommand
from agent_comms.thread_execution import OwnerLifecycleControl
from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, kw_only=True)
class StopCliCommand(AppliedTogether, NativeOwnerCommand, OwnerLifecycleControl,
                     ThreadLifecycleCliCommand, CliCommand[ThreadStoppedResult]):
    help = "Stop process"

    def apply(self, ctx: Comms) -> ThreadStoppedResult:
        return self.apply_together(ctx, (self,))[0]

    @classmethod
    def apply_together(cls, ctx: Comms, commands: tuple[Self, ...]) -> tuple[ThreadStoppedResult, ...]:
        """Every selected owner shares one signal pass and one grace period."""
        ctx.owners.stop(*(command.name for command in commands))
        return tuple(ThreadStoppedResult(command.name) for command in commands)
