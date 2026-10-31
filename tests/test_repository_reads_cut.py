"""One pass's repository cut against worktrees that appear or vanish while it reads."""
import subprocess
from pathlib import Path

import pytest
from fixture_git import IDENTITY

from agent_comms.repo import Checkout, FailedGitCommand, RepositoryReads


def git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *IDENTITY, *arguments], cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def main(tmp_path: Path) -> Path:
    path = tmp_path / "main"
    path.mkdir()
    git(path, "init", "--initial-branch=main")
    git(path, "commit", "--allow-empty", "-m", "first")
    return path


def test_a_worktree_added_after_the_cut_answers_its_own_head(main: Path) -> None:
    reads = RepositoryReads()
    reads.history_for(Checkout(main))
    added = main.parent / "added"
    git(main, "worktree", "add", "-b", "later", str(added))
    assert reads.history_for(Checkout(added)).head_for(Checkout(added)).required() == (git(added, "rev-parse", "HEAD"),)
    assert reads.history_for(Checkout(main)).head_for(Checkout(main)).required() == (git(main, "rev-parse", "HEAD"),)


def test_a_listed_worktree_removed_after_the_cut_keeps_its_cut_head(main: Path) -> None:
    listed = main.parent / "listed"
    git(main, "worktree", "add", "-b", "soon-gone", str(listed))
    head = git(listed, "rev-parse", "HEAD")
    reads = RepositoryReads()
    reads.history_for(Checkout(main))
    resolved = Checkout(listed).resolved()
    git(main, "worktree", "remove", str(listed))
    assert reads.history_for(resolved).head_for(resolved).required() == (head,)


def test_a_worktree_gone_before_the_pass_reads_it_refuses_by_its_git_error(main: Path) -> None:
    gone = main.parent / "gone"
    git(main, "worktree", "add", "-b", "never-read", str(gone))
    reads = RepositoryReads()
    git(main, "worktree", "remove", str(gone))
    with pytest.raises(FailedGitCommand):
        reads.history_for(Checkout(gone))
