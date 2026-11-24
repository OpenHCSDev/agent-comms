from __future__ import annotations

from dataclasses import dataclass

from agent_comms.nominal.field_codec import NullMember
from agent_comms.storage.typed_table import StoreWriter


class TranscriptDraft:
    def end_turn(self, transcript: StoreWriter) -> None:
        """An absent draft needs no write."""

    def publish(self, transcript: StoreWriter) -> None:
        """An absent draft needs no publication."""

    def next_appends[Event](
        self, connection: object, thread: object, turn_id: object, item_id: object,
        events: tuple[Event, ...],
    ) -> tuple[Event, ...]:
        """An absent draft appends every incoming event directly."""
        return events


@dataclass(frozen=True, slots=True)
class NoDraft(NullMember, TranscriptDraft):
    """No external draft: the ACP draft field is absent on the wire."""
