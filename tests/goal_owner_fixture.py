"""Canonical bus setup for owner lifecycle tests with no automatic model work."""

import os
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.active_route import LocalRoute
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.launch_runtime import LaunchRuntime


def canonical_goal_wire(root):
    """Create an empty real certified source for goal/wait fixture operations."""
    comms = Comms(LocalRoute(root), runtime=LaunchRuntime.current())
    comms.messaging.initialize_private_initial_protocol()
    return comms


def activate_empty_source(agent):
    """Use the real issuer on an empty test bus; never migrate existing data."""
    root = agent._comms.root
    root.chmod(0o700)
    issuer = Comms(LocalRoute(root), runtime=LaunchRuntime.current(), private_initial_writes=True)
    with issuer.bus.log.locked():
        metadata = issuer.bus.log.read_metadata_unlocked()
    root_id = (metadata.root_id if metadata.private
               else issuer.messaging.initialize_private_initial_protocol())
    agent._private_nk_wire_root_id = root_id
    agent._private_nk_native_package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    agent.inputs.auto_wake = False
