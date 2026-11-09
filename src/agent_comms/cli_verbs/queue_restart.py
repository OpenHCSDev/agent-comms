from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_support.thread_lifecycle import ThreadLifecycleCliCommand
from agent_comms.cli_verbs.cli_command import TargetActionCommand
from agent_comms.comms import Comms
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.owner_lifecycle import OwnerRestartSelection
from agent_comms.execution.restart_queue import RestartQueue, RestartQueued
from agent_comms.registry.thread_status import ThreadStatus
from agent_comms.registry.threads import Thread


@dataclass(frozen=True, kw_only=True)
class QueueRestartCliCommand(ThreadLifecycleCliCommand, TargetActionCommand[RestartQueued]):
    """A selected owner restarts at its next idle point: at once when idle, never mid-turn."""

    help = "Restart process"

    @classmethod
    def available_for_thread(cls, thread: Thread, status: ThreadStatus) -> bool:
        return bool(thread.restart_candidates(status))

    def apply(self, ctx: Comms) -> RestartQueued:
        selection = OwnerRestartSelection.capture(ctx.registry.snapshot(), self.name)
        return RestartQueued(RestartQueue(ctx.root).enqueue(ctx, selection, launch_runtime=LaunchRuntime.running()))
