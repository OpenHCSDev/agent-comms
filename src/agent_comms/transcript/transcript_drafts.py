from __future__ import annotations

from dataclasses import dataclass

from agent_comms.nominal.field_codec import NullMember
from agent_comms.storage.typed_table import StoreWriter


class TranscriptDraft:
    def end_turn(self, transcript: StoreWriter) -> None:
        """An absent draft needs no write."""

    def publish(self, transcript: StoreWriter) -> None:
        """An absent draft needs no publication."""


@dataclass(frozen=True, slots=True)
class NoDraft(NullMember, TranscriptDraft):
    """No external draft: the ACP draft field is absent on the wire."""
