"""Canonical bus setup for owner lifecycle tests with no automatic model work."""


import shutil

import pytest

from agent_comms.comms import Comms


def canonical_goal_wire(root):
    """Create an empty real checked source for goal/wait fixture operations."""
    comms = Comms(root)
    comms.messaging.initialize_private_initial_protocol()
    return comms


@pytest.fixture
def copied_goal_bus(tmp_path):
    source = canonical_goal_wire(tmp_path / "source")
    target = tmp_path / "copy"
    shutil.copytree(source.root, target)
    return Comms(target)


