from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import MergeReviewResult
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.head_review import PullRequestHead
from agent_comms.repository_name import RepositoryName
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.todos import StoredHeadReview
from agent_comms_hooks import ToolRefusedError
from agent_comms_hooks.queue_bypass import QueueBypass, QueueBypassGuard

from ..cli_bridges.session_watch import WatchClaim


class ReviewedMerge:
    """The CLI and resident hook share exact-head authorization, not a subprocess protocol."""

    def require_actor(self, snapshot: RegistrySnapshot, actor: RecordedThreadIncarnation) -> None:
        if not snapshot.commander_project.by_role(
                snapshot.born(actor.lookup), commander=True, commanded=False):
            raise ToolRefusedError(
                "Only a bound commander session may use a reviewed merge bypass.")

    def reviewed_merges(self, ctx: Comms, snapshot: RegistrySnapshot,
                        actor: RecordedThreadIncarnation,
                        bypasses: tuple[QueueBypass, ...]) -> MergeReviewResult:
        self.require_actor(snapshot, actor)
        heads = []
        for bypass in bypasses:
            target = bypass.target()
            request = PullRequestHead(RepositoryName.from_wire(target.repository), target.number, target.head)
            StoredHeadReview.current_verdict(ctx.goals.todos, request).require_ok(request)
            heads.append(request)
        return MergeReviewResult(tuple(heads))


@dataclass(frozen=True, kw_only=True)
class MergeReviewCliCommand(ReviewedMerge, CliCommand[MergeReviewResult], declared_name="merge-review"):
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
        actor, *others = (snapshot.require(binding.bus).incarnation.require_recorded()
                          for binding in bindings)
        for other in others:
            self.require_actor(snapshot, other)
        return self.reviewed_merges(ctx, snapshot, actor, QueueBypassGuard.bypasses(self.bash))
