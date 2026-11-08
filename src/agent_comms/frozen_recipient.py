"""A frozen audience's member: below the bus tables, so records can decode it during import."""

from __future__ import annotations

from dataclasses import dataclass

from .class_fact import ClassFact
from .nominal.field_codec import ThreadLookup
from .participant_identity import ParticipantIdentity
from .projection_source import ProducedRow, ProjectionSource


@dataclass(frozen=True, slots=True)
class FrozenRecipient(ParticipantIdentity, ProjectionSource):
    """Stable coordination lookup paired with its send-time canonical name."""

    recipient_lookup: ThreadLookup
    canonical_thread: str

    def __post_init__(self) -> None:
        self.require_text(self.recipient_lookup, "recipient_lookup")
        self.require_canonical(self.canonical_thread, "canonical_thread")

    @property
    def lookup(self) -> ThreadLookup:
        return self.recipient_lookup

    @ClassFact
    def writes(cls: type[FrozenRecipient]) -> tuple[type[ProducedRow], ...]:
        from .bus_tables import AwaitingAnswerSource, ReceivedSource

        return (ReceivedSource, AwaitingAnswerSource)
