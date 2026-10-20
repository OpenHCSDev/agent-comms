"""Canonical bus setup for owner lifecycle tests with no automatic model work."""


from agent_comms.comms import Comms


def canonical_goal_wire(root):
    """Create an empty real checked source for goal/wait fixture operations."""
    comms = Comms(root)
    comms.messaging.initialize_private_initial_protocol()
    return comms


