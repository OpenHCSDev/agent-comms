"""The checkpoint command commits and pushes a thread's uncommitted work as a named state."""

import subprocess
from pathlib import Path

import pytest

from agent_comms.cli_verbs.checkpoint import CheckpointCliCommand
from agent_comms.cli_verbs.cli_commands import (
    CleanCheckpoint,
    PushedCheckpoint,
    PushUnknownCheckpoint,
    RefusedCheckpoint,
)
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from bus_record_fixture import context_comms
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


def test_a_dirty_worktree_is_committed_and_pushed_to_the_derived_branch(tmp_path: Path):
    comms = context_comms(tmp_path)
    worktree = _repo(tmp_path, "repo")
    (worktree / "step.py").write_text("print('step')\n")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", "base")
    (worktree / "step.py").write_text("print('step 2')\n")  # uncommitted at compaction
    comms.registry.register(
        NativeThread('Repo', frozenset({'team'}), Checkout(worktree), created_at=17007.0)
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
    # The ancestor repo exists on purpose; the declared non-repo worktree must refuse.
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
        NativeThread('Nested', frozenset({'team'}), Checkout(declared), created_at=17008.0)
    )
    refused = CheckpointCliCommand(name="Nested").apply(comms)
    assert isinstance(refused, RefusedCheckpoint)
    # The ancestor repo is untouched: no checkpoint commit, no staged scratch.
    assert _git(ancestor, "status", "--porcelain").strip().startswith("?? nested/")
    assert _git(ancestor, "rev-parse", "--short", "HEAD").strip() == base


def test_a_stale_declaration_or_broken_transport_never_names_a_fake_push(tmp_path: Path):
    comms = context_comms(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    comms.registry.register(
        NativeThread('NoGit', frozenset({'team'}), Checkout(outside), created_at=17005.0)
    )
    refused = CheckpointCliCommand(name="NoGit").apply(comms)
    assert isinstance(refused, RefusedCheckpoint)
    assert refused.branch == "checkpoint/NoGit"

    broken = _repo(tmp_path, "broken", remote=False)
    _git(broken, "remote", "add", "origin", str(tmp_path / "missing.git"))
    (broken / "left.txt").write_text("uncommitted\n")
    comms.registry.register(
        NativeThread('Broken', frozenset({'team'}), Checkout(broken), created_at=17006.0)
    )
    unknown = CheckpointCliCommand(name="Broken").apply(comms)
    assert isinstance(unknown, PushUnknownCheckpoint)
    assert unknown.branch == "checkpoint/Broken"
    # The commit exists locally even though the push could not be named.
    assert _git(broken, "rev-parse", "--short", "HEAD").strip()


def test_a_nested_clone_is_left_untracked_and_never_adopted(tmp_path: Path):
    """A clone inside the worktree belongs to its own repository. The checkpoint saves this
    thread's work; it does not commit a pointer to somebody else's tree."""
    comms = context_comms(tmp_path)
    worktree = _repo(tmp_path, "repo")
    (worktree / "step.py").write_text("print('step')\n")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", "base")
    (worktree / "step.py").write_text("print('step 2')\n")
    nested = worktree / "skills" / "vendored"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q", "-b", "main")
    (nested / "SKILL.md").write_text("# vendored\n")
    _git(nested, "add", "-A")
    _git(nested, "commit", "-q", "-m", "vendored")
    comms.registry.register(
        NativeThread('Repo', frozenset({'team'}), Checkout(worktree), created_at=17009.0)
    )

    result = CheckpointCliCommand(name="Repo").apply(comms)
    assert isinstance(result, PushedCheckpoint)
    pushed = _git(tmp_path / "repo.origin.git", "ls-tree", "-r", "--name-only", result.branch)
    assert "step.py" in pushed
    assert "skills/vendored" not in pushed
    assert "skills/vendored/SKILL.md" not in pushed
    # The clone stays in the worktree, visible and untracked, owned by nobody here.
    assert _git(worktree, "status", "--porcelain").strip().startswith("?? skills/")
