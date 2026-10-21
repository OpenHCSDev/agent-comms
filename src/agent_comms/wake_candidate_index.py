"""Bounded wake-candidate reads from the bus's indexed frozen recipients."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .bus import Bus
from .bus_publication import Delivered
from .bus_source_page import CandidateQuery
from .field_codec import MalformedRecord
from .wake_policy import WakePolicy


class ProjectionUnavailableError(RuntimeError):
    """Original delivery remains available when the optional projection fails."""


@dataclass(frozen=True, slots=True)
class Candidate:
    source_seq: int
    message_id: str
    recipient_lookup: str
    sender: str
    target: str
    wake_mode: type[WakePolicy]

    @classmethod
    def for_recipient(cls, record: Delivered, lookup: str) -> tuple[Candidate, ...]:
        return tuple(
            cls(record.seq, record.message_id, lookup, record.sender, record.target,
                decision.candidate_wake_mode())
            for declared in record.decisions if declared.recipient == lookup
            for decision in declared.claim_decisions()
        )


@dataclass(frozen=True, slots=True)
class CandidatePage:
    through_seq: int
    entries: tuple[Candidate, ...]
    has_more: bool


@dataclass(frozen=True)
class WakeCandidateIndex:
    """ReceivedSource's (lookup, seq) primary key is the candidate read index.

    The bus append owns its capability projections and SQLite maintains their
    indexes in that same transaction. There is no second store or maintenance.
    """

    bus: Bus

    @property
    def root(self) -> Path:
        return self.bus.path.parent

    @property
    def through_seq(self) -> int:
        return self.bus.revision()

    def page(
        self, *, recipient_lookup: str, after_seq: int,
        required_through_seq: int, limit: int = 32,
    ) -> CandidatePage:
        request = CandidateQuery.capture(
            recipient_lookup=recipient_lookup, after_seq=after_seq,
            required_through_seq=required_through_seq, limit=limit)
        try:
            with self.bus.reading() as source:
                through = source.revision()
                if through < request.required_through_seq:
                    raise ProjectionUnavailableError("requested bus prefix is not committed")
                records = source.received(
                    request.recipient_lookup, after=request.after_seq,
                    before=through + 1, descending=False,
                    limit=request.limit + 1)
                return CandidatePage(
                    through,
                    tuple(candidate for record in records[:request.limit]
                          for candidate in Candidate.for_recipient(record, request.recipient_lookup)),
                    len(records) > request.limit,
                )
        except (OSError, sqlite3.DatabaseError, MalformedRecord) as error:
            raise ProjectionUnavailableError("candidate projection read unavailable") from error
