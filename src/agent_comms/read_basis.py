"""A9: immutable evidence for exactly the messages a client displays.

This is a value contract, not an assertion that fetching a page paints it. A
client acknowledges it only after paint. DM participant identity deliberately
uses ThreadIncarnation; process and turn generations do not alter provenance.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import astuple, dataclass
from typing import TYPE_CHECKING

from .field_codec import FieldCodec
from .thread_identity import ThreadIncarnation

if TYPE_CHECKING:
    from .declarations import RegistrySnapshot


@dataclass(frozen=True, slots=True)
class Conversation:
    target: str = ""
    participants: tuple[ThreadIncarnation, ...] = ()

    def current(self, snapshot: RegistrySnapshot) -> bool:
        return all(participant.current(snapshot) for participant in self.participants)

    def to_wire(self) -> dict:
        """Encode the persisted participant pair format."""
        data = FieldCodec.encode(self)
        data["participants"] = [list(astuple(participant)) for participant in self.participants]
        return data

    @classmethod
    def from_wire(cls, raw: dict) -> Conversation:
        """Decode participant pairs once at the persisted-key boundary."""
        data = dict(raw)
        data["participants"] = [
            FieldCodec.encode(ThreadIncarnation(*FieldCodec.decode(tuple[str, float], item)))
            for item in data.get("participants", [])
        ]
        return FieldCodec.decode(cls, data)


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

    @property
    def viewer_identity(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.viewer, self.viewer_created_at)

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
            or thread.incarnation != self.viewer_identity
            or bus_identity != self.bus_identity
            or any(not item.conversation.current(snapshot) for item in self.conversations)
        ):
            raise ValueError("Display incarnation changed; refresh the displayed page.")
