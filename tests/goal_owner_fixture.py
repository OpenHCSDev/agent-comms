"""Canonical bus setup for owner lifecycle tests with no automatic model work."""

import os
from pathlib import Path

from agent_comms.comms import Comms


def activate_empty_source(agent):
    """Use the real issuer on an empty test bus; never migrate existing data."""
    root = agent._comms.root
    root.chmod(0o700)
    issuer = Comms(root, private_initial_writes=True)
    with issuer.bus.log.locked():
        metadata = issuer.bus.log.read_metadata_unlocked()
    root_id = (metadata.root_id if metadata.private
               else issuer.messaging.initialize_private_initial_protocol())
    agent._private_nk_wire_root_id = root_id
    agent._private_nk_native_package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    agent.inputs.auto_wake = False
