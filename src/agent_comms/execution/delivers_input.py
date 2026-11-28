from abc import ABC, abstractmethod

from agent_comms.cli_bridges.delivery_outcome import Outcome


class DeliversInput[Message](ABC):
    """Places a bus message in front of the runtime's model."""

    @abstractmethod
    async def deliver(self, message: Message) -> type[Outcome]:
        """Return the runtime's evidence of delivery; the caller owns completion."""
