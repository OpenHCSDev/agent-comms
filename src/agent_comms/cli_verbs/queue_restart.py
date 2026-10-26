from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_support.thread_lifecycle import ThreadLifecycleCliCommand
from agent_comms.comms import Comms
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.restart_queue import RestartQueued
from agent_comms.restart_queue import enqueue
from agent_comms.thread_status import ThreadStatus
from agent_comms.threads import Thread
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class QueueRestartCliCommand(ThreadLifecycleCliCommand, CliCommand[RestartQueued]):
    """A selected owner restarts at its next idle point: at once when idle, never mid-turn."""

    help = "Restart process"

    @classmethod
    def available_for_thread(cls, thread: Thread, status: ThreadStatus) -> bool:
        return bool(thread.restart_candidates(status))

    def apply(self, ctx: Comms) -> RestartQueued:
        selection = OwnerRestartSelection.capture(ctx.registry.snapshot(), self.name)
        return RestartQueued(enqueue(ctx, selection, launch_runtime=LaunchRuntime.running()))
