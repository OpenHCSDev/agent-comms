"""A9: immutable evidence for exactly the messages a client displays.

This is a value contract, not an assertion that fetching a page paints it. A
client acknowledges it only after paint. DM participant identity deliberately
uses (name, created_at), not ownership or turn counters; S5 owns richer identity.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .declarations import RegistrySnapshot


@dataclass(frozen=True, slots=True)
class Conversation:
    target: str = ""
    participants: tuple[tuple[str, float], ...] = ()

    def current(self, snapshot: RegistrySnapshot) -> bool:
        return all(
            (
                name not in snapshot.threads
                if created == -1.0
                else name in snapshot.threads and snapshot.threads[name].created_at == created
            )
            for name, created in self.participants
        )


@dataclass(frozen=True, slots=True)
class DisplayedConversation:
    conversation: Conversation
    sequences: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DisplayBasis:
    viewer: str
    viewer_created_at: float
    conversations: tuple[DisplayedConversation, ...]
    bus_identity: tuple[int, int] | None

    def select(self, sequences: Collection[int]) -> DisplayBasis:
        """Retain painted members of this proof; never introduce new sequences."""
        selected = frozenset(sequences)
        return DisplayBasis(
            self.viewer,
            self.viewer_created_at,
            tuple(
                DisplayedConversation(
                    item.conversation, tuple(n for n in item.sequences if n in selected)
                )
                for item in self.conversations
            ),
            self.bus_identity,
        )

    def through(self, sequence: int) -> DisplayBasis:
        if sequence < 0:
            raise ValueError("Display sequence cannot be negative.")
        return DisplayBasis(
            self.viewer,
            self.viewer_created_at,
            tuple(
                DisplayedConversation(
                    item.conversation, tuple(n for n in item.sequences if n <= sequence)
                )
                for item in self.conversations
            ),
            self.bus_identity,
        )

    def validate(
        self, viewer: str, snapshot: RegistrySnapshot, bus_identity: tuple[int, int] | None
    ) -> None:
        thread = snapshot.threads.get(viewer)
        if (
            viewer != self.viewer
            or thread is None
            or thread.created_at != self.viewer_created_at
            or bus_identity != self.bus_identity
            or any(not item.conversation.current(snapshot) for item in self.conversations)
        ):
            raise ValueError("Display incarnation changed; refresh the displayed page.")
