"""The checkpoint command commits and pushes a thread's uncommitted work as a named state."""

import subprocess
from pathlib import Path

import pytest

from agent_comms.cli_commands import (
    CheckpointCliCommand, CleanCheckpoint, PushUnknownCheckpoint, PushedCheckpoint, RefusedCheckpoint,
)
from bus_record_fixture import context_comms
from agent_comms.threads import Thread
from fixture_git import ENVIRONMENT


@pytest.fixture(autouse=True)
def _fixture_identity(monkeypatch):
    """The checkpoint command commits through its own git, so the identity rides the
    environment that git inherits, never a repository's config."""
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


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
    if remote:
        _git(tmp_path, "init", "-q", "--bare", f"{name}.origin.git")
        _git(worktree, "remote", "add", "origin", str(tmp_path / f"{name}.origin.git"))
    return worktree


def test_a_dirty_worktree_is_committed_and_pushed_to_the_derived_branch(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES", raising=False)
    comms = context_comms(tmp_path)
    worktree = _repo(tmp_path, "repo")
    (worktree / "step.py").write_text("print('step')\n")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", "base")
    (worktree / "step.py").write_text("print('step 2')\n")  # uncommitted at compaction
    comms.registry.register(
        Thread('Repo', frozenset({'team'}), str(worktree), created_at=17007.0)
    )

    result = CheckpointCliCommand(name="Repo").apply(comms)
    assert isinstance(result, PushedCheckpoint)
    assert result.branch == "checkpoint/Repo"
    assert _git(tmp_path / "repo.origin.git", "rev-parse", "--short",
                f"refs/heads/{result.branch}").strip() == result.commit
    # The pushed commit carries the previously uncommitted change.
    pushed = subprocess.run(["git", "-C", str(tmp_path / "repo.origin.git"), "show",
                             f"{result.branch}:step.py"], capture_output=True, text=True)
    assert pushed.stdout == "print('step 2')\n"
    # The worktree is now clean; a second checkpoint before compaction is a no-op.
    assert CheckpointCliCommand(name="Repo").apply(comms) == CleanCheckpoint("Repo", "checkpoint/Repo")


@pytest.mark.parametrize("relative", [False, True])
def test_production_git_never_discovers_a_checkout_above_the_declaration(
    tmp_path: Path, monkeypatch, relative: bool
):
    # The production git() carries the ceiling pin itself; the ancestor repo
    # here exists on purpose and the declared non-repo worktree must refuse.
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES", raising=False)
    comms = context_comms(tmp_path)
    ancestor = _repo(tmp_path, "ancestor", remote=False)
    (ancestor / "base.txt").write_text("base\n")
    _git(ancestor, "add", "-A")
    _git(ancestor, "commit", "-q", "-m", "base")
    nested = ancestor / "nested"
    nested.mkdir()
    (nested / "scratch.txt").write_text("uncommitted\n")
    base = _git(ancestor, "rev-parse", "--short", "HEAD").strip()
    monkeypatch.chdir(tmp_path)
    declared = nested.relative_to(tmp_path) if relative else nested
    comms.registry.register(
        Thread('Nested', frozenset({'team'}), str(declared), created_at=17008.0)
    )
    refused = CheckpointCliCommand(name="Nested").apply(comms)
    assert isinstance(refused, RefusedCheckpoint)
    # The ancestor repo is untouched: no checkpoint commit, no staged scratch.
    assert _git(ancestor, "status", "--porcelain").strip().startswith("?? nested/")
    assert _git(ancestor, "rev-parse", "--short", "HEAD").strip() == base


def test_a_stale_declaration_or_broken_transport_never_names_a_fake_push(
    tmp_path: Path, monkeypatch
):
    # The production git() owns the ceiling pin; no test-side pin is needed.
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES", raising=False)
    comms = context_comms(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    comms.registry.register(
        Thread('NoGit', frozenset({'team'}), str(outside), created_at=17005.0)
    )
    refused = CheckpointCliCommand(name="NoGit").apply(comms)
    assert isinstance(refused, RefusedCheckpoint)
    assert refused.branch == "checkpoint/NoGit"

    broken = _repo(tmp_path, "broken", remote=False)
    _git(broken, "remote", "add", "origin", str(tmp_path / "missing.git"))
    (broken / "left.txt").write_text("uncommitted\n")
    comms.registry.register(
        Thread('Broken', frozenset({'team'}), str(broken), created_at=17006.0)
    )
    unknown = CheckpointCliCommand(name="Broken").apply(comms)
    assert isinstance(unknown, PushUnknownCheckpoint)
    assert unknown.branch == "checkpoint/Broken"
    # The commit exists locally even though the push could not be named.
    assert _git(broken, "rev-parse", "--short", "HEAD").strip()
