from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import (
    CleanCheckpoint,
    FailedCheckpoint,
    NameArgument,
    PushedCheckpoint,
    PushUnknownCheckpoint,
    RefusedCheckpoint,
)
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.projects.git_errors import GitCommandError
from agent_comms.projects.repo import Repo


def declared_submodules(worktree: Path) -> frozenset[str]:
    """The submodule paths this repository declares in its own .gitmodules."""
    modules = worktree / ".gitmodules"
    if not modules.is_file():
        return frozenset()
    paths = {
        value.strip()
        for line in modules.read_text().splitlines()
        if (key := line.partition("=")[0].strip()) == "path" and (value := line.partition("=")[2])
    }
    return frozenset(paths)


def embedded_repositories(worktree: Path) -> tuple[str, ...]:
    """Paths git staged as a link to another repository that this one does not declare.

    A nested clone is that project's work, not this thread's: adopting it commits a
    pointer to somebody else's tree, and the checkout keeps a second identity."""
    declared = declared_submodules(worktree)
    embedded = []
    for entry in Repo.git(worktree, "ls-files", "--stage").splitlines():
        mode, _, rest = entry.partition(" ")
        if mode != "160000":
            continue
        path = rest.partition("\t")[2]
        if path and path not in declared:
            embedded.append(path)
    return tuple(embedded)


@dataclass(frozen=True, kw_only=True)
class CheckpointCliCommand(NameArgument, CliCommand, declared_name="checkpoint"):
    help = "Commit and push a thread's uncommitted worktree changes as a checkpoint branch"

    def apply(self, ctx: Comms) -> CliResult:
        worktree = ctx.registry.require(self.name).worktree.path.resolve()
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
            embedded = embedded_repositories(worktree)
            if embedded:
                # Left untracked in the worktree: visible, never adopted.
                Repo.git(worktree, "reset", "-q", "--", *embedded)
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
