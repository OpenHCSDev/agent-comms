from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import ActiveStatusResult
from agent_comms.cli_commands import ThreadActivityRow
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class StatusCliCommand(CliCommand):
    help = "Live activity: what each thread is doing right now"

    def apply(self, ctx: Comms) -> CliResult:
        snapshot = ctx.registry.snapshot()
        running = snapshot.active_threads(snapshot.active_candidates())
        activities = ctx.agents.all_activity(running, snapshot=snapshot)
        return ActiveStatusResult(tuple(
            ThreadActivityRow(thread, activity.state, activity.detail, activity.timestamp)
            for thread, activity in sorted(activities.items())))
