"""Canonical bus setup for owner lifecycle tests with no automatic model work."""
from agent_comms.native_package import pinned_native_package


from agent_comms.comms import Comms


def canonical_goal_wire(root):
    """Create an empty real certified source for goal/wait fixture operations."""
    comms = Comms(root)
    comms.messaging.initialize_private_initial_protocol()
    return comms


def activate_empty_source(agent):
    """Use the real issuer on an empty test bus; never migrate existing data."""
    root = agent._comms.root
    root.chmod(0o700)
    issuer = Comms(root, private_initial_writes=True)
    with issuer.bus.log.locked():
        metadata = issuer.bus.log.read_metadata_unlocked()
    root_id = (metadata.root_id if metadata.private
               else issuer.messaging.initialize_private_initial_protocol())
    agent._comms.owners.pin_private_nk_launch(root, root_id, pinned_native_package())
    agent.inputs.auto_wake = False
