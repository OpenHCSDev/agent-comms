"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import CleanCheckpoint
from agent_comms.cli_commands import FailedCheckpoint
from agent_comms.cli_commands import NameArgument
from agent_comms.cli_commands import PushUnknownCheckpoint
from agent_comms.cli_commands import PushedCheckpoint
from agent_comms.cli_commands import RefusedCheckpoint
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.repo import GitCommandError
from agent_comms.repo import Repo
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class CheckpointCliCommand(NameArgument, CliCommand, declared_name="checkpoint"):
    help = "Commit and push a thread's uncommitted worktree changes as a checkpoint branch"

    def apply(self, ctx: Comms) -> CliResult:
        worktree = Path(ctx.registry.require(self.name).worktree).resolve()
        branch = f"checkpoint/{self.name}"
        # Only the declared worktree's own repository; never checkpoint an ancestor's.
        refused = RefusedCheckpoint(
            self.name, branch, "the thread declaration's worktree is not a git worktree")
        try:
            top = Path(Repo.git(worktree, "rev-parse", "--show-toplevel").strip()).resolve()
        except GitCommandError:
            return refused
        if top != worktree:
            return refused
        if not Repo.git(worktree, "status", "--porcelain").strip():
            return CleanCheckpoint(self.name, branch)
        try:
            Repo.git(worktree, "add", "-A")
            if Repo.answers(worktree, "diff", "--cached", "--quiet"):
                return CleanCheckpoint(self.name, branch)
            Repo.git(worktree, "commit", "-m", f"Checkpoint {self.name} before compaction")
        except GitCommandError as error:
            return FailedCheckpoint(self.name, branch, str(error)[:400])
        # The transport can fail mid-flight; the named state is what survives,
        # never a silent skip and never a block on compaction.
        try:
            Repo.git(worktree, "push", "--force", "origin", f"HEAD:refs/heads/{branch}",
                     timeout=120)
        except GitCommandError as error:
            return PushUnknownCheckpoint(self.name, branch, str(error)[:400])
        return PushedCheckpoint(self.name, branch,
                                Repo.git(worktree, "rev-parse", "--short", "HEAD").strip())
