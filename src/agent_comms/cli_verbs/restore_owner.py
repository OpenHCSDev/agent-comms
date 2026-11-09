from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from agent_comms.cli_support.thread_lifecycle import ThreadLifecycleCliCommand
from agent_comms.cli_verbs.cli_command import AppliedTogether, TargetActionCommand
from agent_comms.comms import Comms
from agent_comms.execution.owner_lifecycle import OwnerStartResult
from agent_comms.registry.threads import NativeOwnerCommand, OwnerLifecycleControl


@dataclass(frozen=True, kw_only=True)
class RestoreOwnerCliCommand(AppliedTogether[OwnerStartResult], NativeOwnerCommand, OwnerLifecycleControl,
                             ThreadLifecycleCliCommand, TargetActionCommand[OwnerStartResult],
                             declared_name="restore-owner"):
    help = "Restore owner process without changing its goal"

    @classmethod
    def apply_together(cls, ctx: Comms, commands: tuple[Self, ...]) -> tuple[OwnerStartResult, ...]:
        return ctx.owners.start_many(tuple(command.name for command in commands))
