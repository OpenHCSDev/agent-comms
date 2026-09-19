"""Shared fixtures: an isolated Comms wire per test."""

from pathlib import Path

import pytest

from agent_comms import Thread
from agent_comms.operations import Comms


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "comms"


@pytest.fixture
def comms(root: Path) -> Comms:
    return Comms(root)


@pytest.fixture
def wired(comms: Comms) -> Comms:
    """A wire with PR111 (parent) and fixer (child) registered."""
    comms.register(Thread(name="PR111", tags=frozenset({"base"}), worktree="/tmp/wt1", pid=100))
    comms.register(
        Thread(
            name="fixer",
            tags=frozenset({"auth"}),
            worktree="/tmp/wt1",
            parent="PR111",
            task="fix auth",
            pid=200,
        )
    )
    return comms
