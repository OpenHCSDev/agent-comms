from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from agent_comms.cli_support.thread_lifecycle import ThreadLifecycleCliCommand
from agent_comms.cli_verbs.cli_command import AppliedTogether, TargetActionCommand
from agent_comms.cli_verbs.cli_commands import ThreadStoppedResult
from agent_comms.comms import Comms
from agent_comms.registry.threads import NativeOwnerCommand, OwnerLifecycleControl


@dataclass(frozen=True, kw_only=True)
class StopCliCommand(AppliedTogether[ThreadStoppedResult], NativeOwnerCommand, OwnerLifecycleControl,
                     ThreadLifecycleCliCommand, TargetActionCommand[ThreadStoppedResult]):
    help = "Stop process"

    @classmethod
    def apply_together(cls, ctx: Comms, commands: tuple[Self, ...]) -> tuple[ThreadStoppedResult, ...]:
        """Every selected owner shares one signal pass and one grace period."""
        ctx.owners.stop(*(command.name for command in commands))
        return tuple(ThreadStoppedResult(command.name) for command in commands)
