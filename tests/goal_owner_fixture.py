"""Canonical bus setup for owner lifecycle tests with no automatic model work."""

from agent_comms.bus import Bus


from agent_comms.bus import Bus
from agent_comms.comms import Comms


def canonical_goal_wire(root):
    """Create an empty real checked source for goal/wait fixture operations."""
    Bus.install(Bus.path_in(root))
    comms = Comms(root)
    Bus.install(Bus.path_in(comms.root))
    return comms


