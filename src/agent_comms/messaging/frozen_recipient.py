"""A frozen audience's member: below the bus tables, so records can decode it during import."""

from __future__ import annotations

from dataclasses import dataclass

from agent_comms.execution.participant_identity import ParticipantIdentity
from agent_comms.messaging.projection_source import ProducedRow, ProjectionSource
from agent_comms.nominal.class_fact import ClassFact
from agent_comms.nominal.field_codec import ThreadLookup


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
        from agent_comms.messaging.bus_tables import AwaitingAnswerSource, ReceivedSource

        return (ReceivedSource, AwaitingAnswerSource)
