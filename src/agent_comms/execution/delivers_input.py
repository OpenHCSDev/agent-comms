from abc import ABC, abstractmethod

from agent_comms.cli_bridges.harness_delivery_states import Outcome


class DeliversInput[Message](ABC):
    """Places a bus message in front of the runtime's model."""

    @abstractmethod
    async def deliver(self, message: Message) -> type[Outcome]:
        """Return the runtime's evidence of delivery; the caller owns completion."""
