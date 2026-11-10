from __future__ import annotations

from abc import ABC, abstractmethod

from agent_comms.registry.threads import Thread


class RecipientSelection(ABC):
    """One registry selection owns live, archived and retired recipient behavior."""

    def activity_threads(self) -> tuple[Thread, ...]:
        return ()

    @abstractmethod
    def activity(self, observations, snapshot): ...

    @abstractmethod
    def read_effect(self, reads, message, snapshot, *, document=None): ...

    def input_owners(self) -> tuple[Thread, ...]:
        """The live thread whose stored inputs this recipient's deliveries are keyed by."""
        return ()
