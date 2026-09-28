"""Shared fixtures: an isolated Comms wire per test."""

from pathlib import Path

import pytest

from agent_comms.operations import Comms
from agent_comms.threads import Thread


@pytest.fixture(autouse=True)
def _deferred_candidate_scheduler_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep optional background WAL workers out of unrelated test temp cleanup.

    tests/test_candidate_maintenance.py opts back in for the production hook.
    This does not affect fresh subprocess processes used by integration tests.
    """
    monkeypatch.setattr(
        "agent_comms.operations.schedule_private_candidate_after_commit", lambda *_: None
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "comms"


@pytest.fixture
def comms(root: Path) -> Comms:
    return Comms(root)


@pytest.fixture
def wired(comms: Comms) -> Comms:
    """A wire with PR111 (parent) and fixer (child) registered."""
    comms.register(Thread(name="PR111", tags=frozenset({"base"}), worktree="/tmp/wt1"))
    comms.register(
        Thread(
            name="fixer",
            tags=frozenset({"auth"}),
            worktree="/tmp/wt1",
            parent="PR111",
            task="fix auth",
            pid=0,
        )
    )
    return comms
