from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import MergeReviewResult
from agent_comms.comms import Comms
from agent_comms.errors import ToolRefusedError
from agent_comms.field_codec import Argument
from agent_comms.head_review import PullRequestHead
from agent_comms.repository_name import RepositoryName
from agent_comms.shell_commands import ShellCommand
from agent_comms.todos import StoredHeadReview
from agent_comms_hooks.queue_bypass import QueueBypass
from agent_comms_hooks.watch import WatchClaim


@dataclass(frozen=True, kw_only=True)
class MergeReviewCliCommand(CliCommand[MergeReviewResult], declared_name="merge-review"):
    """Read the existing exact-head verdict for a commander session's merge command."""

    help = "Read Core's recorded decision for the pinned admin/API merge head"
    session: Annotated[str, Argument(flags=("--session",))]
    bash: Annotated[str, Argument(flags=("--bash",))]

    def apply(self, ctx: Comms) -> MergeReviewResult:
        bindings = tuple(binding for binding in WatchClaim(self.session).state().bindings()
                         if Path(binding.root) == ctx.root)
        if not bindings:
            raise ToolRefusedError("The commander session has no recorded binding to this bus; no verdict can be read.")
        snapshot = ctx.registry.snapshot()
        for binding in bindings:
            subject = snapshot.require(binding.bus)
            if not any(subject.incarnation == commander.incarnation
                       for commander in snapshot.commander_project.commanders(snapshot)):
                raise ToolRefusedError("Only a bound commander session may use a reviewed merge bypass.")
        heads = []
        for simple in ShellCommand.split(self.bash):
            for bypass in QueueBypass.matching(simple):
                target = bypass.target()
                request = PullRequestHead(RepositoryName.from_wire(target.repository), target.number, target.head)
                project = snapshot.commander_project.require()
                StoredHeadReview.current_verdict(ctx.goals.todos, request).require_merge(request, project.repos)
                heads.append(request)
        return MergeReviewResult(tuple(heads))
