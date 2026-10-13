"""The checkpoint command commits and pushes a thread's uncommitted work as a named state."""

import os
import subprocess
from pathlib import Path

from agent_comms.cli_commands import CheckpointCliCommand
from agent_comms.threads import Thread
from test_private_bus_checkpoint import _root


def _git(worktree: Path, *arguments: str):
    done = subprocess.run(["git", "-C", str(worktree), *arguments],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _repo(tmp_path: Path, name: str, *, remote: bool = True) -> Path:
    """A thread worktree that is a real git repo, never an ancestor's worktree."""
    worktree = tmp_path / name
    worktree.mkdir()
    _git(worktree, "init", "-q", "-b", "main")
    _git(worktree, "config", "user.name", "agent")
    _git(worktree, "config", "user.email", "agent@example.com")
    if remote:
        _git(tmp_path, "init", "-q", "--bare", f"{name}.origin.git")
        _git(worktree, "remote", "add", "origin", str(tmp_path / f"{name}.origin.git"))
    return worktree


def test_a_dirty_worktree_is_committed_and_pushed_to_the_derived_branch(
    tmp_path: Path, monkeypatch
):
    # Temp dirs live inside a checkout; git must never discover that ancestor.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    comms, _ = _root(tmp_path)
    worktree = _repo(tmp_path, "repo")
    (worktree / "step.py").write_text("print('step')\n")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", "base")
    (worktree / "step.py").write_text("print('step 2')\n")  # uncommitted at compaction
    comms.registry.register(
        Thread("Repo", frozenset({"team"}), str(worktree), created_at=17007.0)
    )

    result = CheckpointCliCommand(name="Repo").apply(comms)
    assert result["checkpoint"] == "pushed"
    assert result["branch"] == "checkpoint/Repo"
    assert _git(tmp_path / "repo.origin.git", "rev-parse", "--short",
                f"refs/heads/{result['branch']}").strip() == result["commit"]
    # The pushed commit carries the previously uncommitted change.
    pushed = subprocess.run(["git", "-C", str(tmp_path / "repo.origin.git"), "show",
                             f"{result['branch']}:step.py"], capture_output=True, text=True)
    assert pushed.stdout == "print('step 2')\n"
    # The worktree is now clean; a second checkpoint before compaction is a no-op.
    assert CheckpointCliCommand(name="Repo").apply(comms) == {
        "thread": "Repo", "checkpoint": "clean", "branch": "checkpoint/Repo",
    }


def test_a_stale_declaration_or_broken_transport_never_names_a_fake_push(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    comms, _ = _root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    comms.registry.register(
        Thread("NoGit", frozenset({"team"}), str(outside), created_at=17005.0)
    )
    refused = CheckpointCliCommand(name="NoGit").apply(comms)
    assert refused["checkpoint"] == "refused"
    assert refused["branch"] == "checkpoint/NoGit"

    broken = _repo(tmp_path, "broken", remote=False)
    _git(broken, "remote", "add", "origin", str(tmp_path / "missing.git"))
    (broken / "left.txt").write_text("uncommitted\n")
    comms.registry.register(
        Thread("Broken", frozenset({"team"}), str(broken), created_at=17006.0)
    )
    unknown = CheckpointCliCommand(name="Broken").apply(comms)
    assert unknown["checkpoint"] == "push-unknown"
    assert unknown["branch"] == "checkpoint/Broken"
    # The commit exists locally even though the push could not be named.
    assert _git(broken, "rev-parse", "--short", "HEAD").strip()
