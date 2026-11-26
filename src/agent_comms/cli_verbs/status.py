from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_verbs.cli_verb import CliBus, CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.registry.activity import ActivityState


@dataclass(frozen=True)
class ThreadActivityRow:
    thread: str
    state: ActivityState
    detail: str
    ts: float


@dataclass(frozen=True)
class ActiveStatusResult(CliResult):
    status: tuple[ThreadActivityRow, ...]


@dataclass(frozen=True, kw_only=True)
class StatusCliCommand(CliCommand):
    help = "Live activity: what each thread is doing right now"

    def apply(self, ctx: CliBus) -> CliResult:
        snapshot = ctx.registry.snapshot()
        running = snapshot.active_threads(snapshot.active_candidates())
        activities = ctx.agents.all_activity(running, snapshot=snapshot)
        return ActiveStatusResult(tuple(
            ThreadActivityRow(thread, activity.state, activity.detail, activity.timestamp)
            for thread, activity in sorted(activities.items())))
