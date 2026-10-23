"""Canonical bus setup for owner lifecycle tests with no automatic model work."""


import shutil

import pytest

from agent_comms.bus import Bus
from agent_comms.comms import wire


def canonical_goal_wire(root):
    """Create an empty real checked source for goal/wait fixture operations."""
    comms = wire(root)
    Bus.install(Bus.path_in(comms.root))
    return comms


@pytest.fixture
def copied_goal_bus(tmp_path):
    source = canonical_goal_wire(tmp_path / "source")
    target = tmp_path / "copy"
    shutil.copytree(source.root, target)
    return wire(target)


