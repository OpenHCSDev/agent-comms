"""Disposable wake candidates, indexed in bounded batches by bus sequence.

These rows are hints, never authority to deliver or start a model turn.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from .bus import Bus
from .bus_publication import Delivered
from .bus_source_page import CandidateQuery
from .field_codec import FieldCodec, Id128Text, MalformedRecord
from .mro_dispatch import MroDispatch, handles
from .typed_table import Column, DerivedStore, Index, TypedTable
from .wake_policy import WakePolicy


class ProjectionUnavailableError(RuntimeError):
    """Original delivery remains available when the optional projection fails."""


class ProjectionRebuildRequiredError(ProjectionUnavailableError):
    """The disposable projection requires explicit rebuilding."""


class CandidateTable:
    """Derived candidate rows and the progress of their one maintenance pass."""


@dataclass(frozen=True)
class CandidateScan(CandidateTable, TypedTable):
    singleton: Annotated[Literal[1], Column(primary_key=True)]
    through_seq: Annotated[int, Column(check="through_seq>=0")]

    @classmethod
    def through(cls, db: sqlite3.Connection) -> int:
        return next((row.through_seq for row in cls.select(db)), 0)


@dataclass(frozen=True)
class Candidate(CandidateTable, TypedTable):
    source_seq: Annotated[int, Column(primary_key=True)]
    message_id: str
    recipient_lookup: Annotated[str, Column(primary_key=True)]
    sender: str
    target: str
    wake_mode: type[WakePolicy] | None
    without_rowid = True
    indexes = (
        Index(("recipient_lookup", "source_seq"), where="wake_mode IS NOT NULL"),
        Index(("recipient_lookup", "source_seq"), where="wake_mode IS NULL"),
    )


@dataclass(frozen=True, slots=True)
class CandidatePage:
    through_seq: int
    entries: tuple[Candidate, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class CommittedAppendHint:
    """Untrusted post-commit scheduling hint, not delivery authority."""

    root_id: Annotated[str, Id128Text]
    seq: int

    def __post_init__(self) -> None:
        if self.seq <= 0:
            raise ValueError("append hint requires a positive committed sequence")

    @classmethod
    def capture(cls, root_id: str, seq: int) -> CommittedAppendHint:
        return FieldCodec.decode(cls, {"root_id": root_id, "seq": seq})

    def validated(self) -> CommittedAppendHint:
        return FieldCodec.decode(type(self), FieldCodec.encode(self))


class WakeCandidateIndex(DerivedStore, MroDispatch):
    """One bounded scan; candidate inserts and scanned-through seq commit together."""

    batch_rows = 64
    rows = tuple(TypedTable.members_with(CandidateTable))
    schema_error = ProjectionRebuildRequiredError

    def __init__(self, bus: Bus):
        self.bus = bus

    @property
    def root(self) -> Path:
        return self.bus.path.parent

    @staticmethod
    def _validate_limits(max_rows: int) -> None:
        if type(max_rows) is not int or not 1 <= max_rows <= 256:
            raise ValueError("max_rows must be between 1 and 256")

    @handles(Delivered)
    def _insert_candidates(self, record: Delivered, db: sqlite3.Connection) -> None:
        for recipient, decision in zip(record.audience.recipients, record.decisions, strict=True):
            Candidate(record.seq, record.message_id, recipient.recipient_lookup,
                      record.sender, record.target, decision.candidate_wake_mode()).insert(db)

    @property
    def through_seq(self) -> int:
        with self.snapshot() as db:
            return CandidateScan.through(db)

    def maintain(self, *, max_rows: int = batch_rows) -> int:
        """Read one keyset page outside the derived writer; commit its exact progress."""
        self._validate_limits(max_rows)
        prior = self.through_seq
        records = self.bus.since(prior, max_rows)
        with self.writing() as db:
            current = CandidateScan.through(db)
            if current != prior:
                return current
            for record in records:
                self.dispatch_sync(record, db)
            through = records[-1].seq if records else prior
            CandidateScan(1, through).upsert(db)
            return through

    def rebuild(self, *, max_rows: int = batch_rows) -> int:
        """Discard the disposable projection, then start its bounded scan from zero."""
        self._validate_limits(max_rows)
        with self.writing() as db:
            Candidate.delete(db)
            CandidateScan.delete(db)
        return self.maintain(max_rows=max_rows)

    def notify_committed_append(self, *, root_id: str, seq: int) -> CommittedAppendHint:
        """Schedule only after Bus.append commits and outer locks release."""
        return CommittedAppendHint.capture(root_id, seq)

    def catch_up_committed_append(
        self, hint: CommittedAppendHint, *, max_rows: int = batch_rows,
    ) -> bool:
        """Do at most one bounded batch; report whether this committed seq was scanned."""
        hint = hint.validated()
        self._validate_limits(max_rows)
        if self.through_seq >= hint.seq:
            return True
        return self.maintain(max_rows=max_rows) >= hint.seq

    def page(
        self, *, root_id: str, recipient_lookup: str, after_seq: int,
        required_through_seq: int, limit: int = 32,
    ) -> CandidatePage:
        """Read one bounded page; consumers still verify delivery and live ownership."""
        request = CandidateQuery.capture(
            root_id=root_id, recipient_lookup=recipient_lookup, after_seq=after_seq,
            required_through_seq=required_through_seq, limit=limit)
        try:
            with self.snapshot() as db:
                through = CandidateScan.through(db)
                if through < request.required_through_seq:
                    raise ProjectionUnavailableError("candidate projection has not caught up")
                data = Candidate.select(
                    db, where="{source_seq}>? AND {wake_mode} IS NOT NULL",
                    parameters=(request.after_seq,), recipient_lookup=request.recipient_lookup,
                    order_by=("source_seq",), limit=request.limit + 1)
                return CandidatePage(through, tuple(data[:request.limit]), len(data) > request.limit)
        except (OSError, sqlite3.DatabaseError, MalformedRecord) as error:
            raise ProjectionUnavailableError("candidate projection read unavailable") from error
