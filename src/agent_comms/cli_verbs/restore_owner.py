"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_verbs.thread_lifecycle import ThreadLifecycleCliCommand
from agent_comms.comms import Comms
from agent_comms.owner_lifecycle import OwnerStartResult
from agent_comms.thread_execution import NativeOwnerCommand
from agent_comms.thread_execution import OwnerLifecycleControl
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class RestoreOwnerCliCommand(NativeOwnerCommand, OwnerLifecycleControl, ThreadLifecycleCliCommand,
                             CliCommand[OwnerStartResult], declared_name="restore-owner"):
    help = "Restore owner process without changing its goal"

    def apply(self, ctx: Comms) -> OwnerStartResult:
        return ctx.owners.start(self.name)
