"""Original relationship records shared by context and relationship assembly."""

from __future__ import annotations

from dataclasses import dataclass

from agent_comms.peer_relevance import PeerRelevance
from agent_comms.registry.thread_identity import ThreadIncarnation


@dataclass(frozen=True, slots=True)
class RelationRevision:
    """An earlier note of a relation and when it was replaced or written."""

    note: str
    at: float


@dataclass(frozen=True, slots=True)
class RelationRecord:
    """One relation as a thread reads it: who made it, when, why and how it changed."""

    peer: str
    relevance: type[PeerRelevance]
    author: str
    # None when the relation records no time of its own.
    created_at: float | None
    note: str
    history: tuple[RelationRevision, ...]


@dataclass(frozen=True, slots=True)
class PeerRelation:
    """How much one peer matters to a thread, and why."""

    peer: ThreadIncarnation
    relevance: type[PeerRelevance]
    records: tuple[RelationRecord, ...] = ()

    @property
    def note(self) -> str:
        return "\n".join(record.note for record in self.records if record.note)

    @staticmethod
    def order(first: PeerRelation, second: PeerRelation) -> int:
        """More derived relevance first, read from the hierarchy; then name."""
        if first.relevance is not second.relevance:
            if issubclass(first.relevance, second.relevance):
                return -1
            if issubclass(second.relevance, first.relevance):
                return 1
        return (first.peer.name > second.peer.name) - (first.peer.name < second.peer.name)
