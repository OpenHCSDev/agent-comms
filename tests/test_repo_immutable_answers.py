"""A question whose answer cannot change is asked once; one that names a ref keeps asking."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_comms.repo import Checkout, GitCommandError, Repo


def _commit(checkout: Path, message: str) -> str:
    Repo.git(checkout, "-c", "user.name=t", "-c", "user.email=t@t", "commit",
             "--allow-empty", "-m", message)
    return Repo.git(checkout, "rev-parse", "HEAD").strip()


def test_commit_ancestry_is_answered_once_and_a_ref_follows_its_branch(tmp_path: Path) -> None:
    checkout = tmp_path / "repository"
    checkout.mkdir()
    Repo.git(checkout, "init", "-q")
    older = _commit(checkout, "older")
    newer = _commit(checkout, "newer")
    other = checkout.parent / "linked"
    Repo.git(checkout, "worktree", "add", "-q", "--detach", str(other), older)

    assert Repo.descends(checkout, older, newer)
    assert not Repo.descends(checkout, newer, older)
    before = Repo.commit_descends.cache_info()
    assert Repo.descends(other, older, newer), "a linked worktree shares its common directory's answer"
    assert not Repo.descends(other, newer, older)
    after = Repo.commit_descends.cache_info()
    assert (after.misses, after.hits) == (before.misses, before.hits + 2)

    assert not Repo.descends(other, newer, "HEAD")
    Repo.git(other, "checkout", "-q", "--detach", newer)
    assert Repo.descends(other, newer, "HEAD"), "HEAD moved, so its answer must too"
    assert Repo.commit_descends.cache_info() == after, "a ref never enters the answer cache"


def test_a_removed_worktree_stops_answering_liveness_after_ancestry_was_asked(tmp_path: Path) -> None:
    checkout = tmp_path / "repository"
    checkout.mkdir()
    Repo.git(checkout, "init", "-q")
    head = _commit(checkout, "only")
    other = checkout.parent / "linked"
    Repo.git(checkout, "worktree", "add", "-q", "--detach", str(other), head)
    assert Repo.descends(other, head, head)

    Repo.git(checkout, "worktree", "remove", str(other))

    with pytest.raises(GitCommandError):
        Checkout(other).common_dir
