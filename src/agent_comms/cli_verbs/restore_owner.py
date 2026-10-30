from __future__ import annotations

from agent_comms.cli_command import AppliedTogether, CliCommand
from agent_comms.cli_support.thread_lifecycle import ThreadLifecycleCliCommand
from agent_comms.comms import Comms
from agent_comms.owner_lifecycle import OwnerStartResult
from agent_comms.thread_execution import NativeOwnerCommand
from agent_comms.thread_execution import OwnerLifecycleControl
from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, kw_only=True)
class RestoreOwnerCliCommand(AppliedTogether, NativeOwnerCommand, OwnerLifecycleControl, ThreadLifecycleCliCommand,
                             CliCommand[OwnerStartResult], declared_name="restore-owner"):
    help = "Restore owner process without changing its goal"

    def apply(self, ctx: Comms) -> OwnerStartResult:
        return self.apply_together(ctx, (self,))[0]

    @classmethod
    def apply_together(cls, ctx: Comms, commands: tuple[Self, ...]) -> tuple[OwnerStartResult, ...]:
        return ctx.owners.start_many(tuple(command.name for command in commands))
