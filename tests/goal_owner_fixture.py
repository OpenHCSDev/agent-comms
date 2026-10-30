"""Canonical bus setup for owner lifecycle tests with no automatic model work."""


import shutil

import pytest

from agent_comms.comms import wire
from agent_comms.storage_rules import StorageRule


def canonical_goal_wire(root):
    """Create an empty real checked source for goal/wait fixture operations."""
    comms = wire(root)
    comms.registry.storage_rule = StorageRule(
        volume=root.parent, checkout_base=root.parent,
        scratch_base=root.parent / "scratch",
    )
    return comms


@pytest.fixture
def copied_goal_bus(tmp_path):
    source = canonical_goal_wire(tmp_path / "source")
    target = tmp_path / "copy"
    shutil.copytree(source.root, target)
    return canonical_goal_wire(target)


