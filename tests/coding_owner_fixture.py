"""The coding owner CodingToolSocket tests stand in for the real CodingToolOwner."""

import secrets

from agent_comms.registry.thread_identity import UnobservedTools


class OwnerDouble:
    """A coding owner whose thread was offered nothing beyond the declared tools; each
    test overrides only how it answers ``allow``."""

    offered = UnobservedTools()

    def __init__(self) -> None:
        self.input_id = secrets.token_hex(16)
