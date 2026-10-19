"""Canonical bus setup for owner lifecycle tests with no automatic model work."""


from agent_comms.comms import Comms


def canonical_goal_wire(root):
    """Create an empty real checked source for goal/wait fixture operations."""
    comms = Comms(root)
    comms.messaging.initialize_private_initial_protocol()
    return comms


def activate_empty_source(agent):
    """Use the real issuer on an empty test bus; never migrate existing data."""
    root = agent._comms.root
    root.chmod(0o700)
    issuer = Comms(root)
    with issuer.bus.log.locked():
        metadata = issuer.bus.log.read_metadata_unlocked()
    if not metadata.private:
        issuer.messaging.initialize_private_initial_protocol()
    agent.inputs.auto_wake = False
