from __future__ import annotations

from typing import TYPE_CHECKING

from abc import ABC, abstractmethod

from agent_comms.registry.threads import Thread

if TYPE_CHECKING:
    from agent_comms.registry.thread_identity import ThreadIncarnation


class RecipientSelection(ABC):
    """One registry selection owns live, archived and retired recipient behavior."""

    def activity_threads(self) -> tuple[Thread, ...]:
        return ()

    @abstractmethod
    def activity(self, observations, snapshot): ...

    @abstractmethod
    def read_effect(self, reads, message, snapshot, *, document=None) -> tuple[ThreadIncarnation, ...]:
        """The readers whose display of `message` this recipient records: a tuple, never a scalar."""

    def input_owners(self) -> tuple[Thread, ...]:
        """The live thread whose stored inputs this recipient's deliveries are keyed by."""
        return ()
