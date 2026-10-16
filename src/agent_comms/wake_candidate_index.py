"""Disposable WAL index of private N/K *candidates*, never wake authority.

Maintenance explicitly replays a bounded bus prefix; a page reads SQLite WAL
plus a 4 KiB prefix-tail fingerprint, never the whole wire or a bus lock.
Pages never claim that bus decisions are sealed coordinator receipts.
The original bus, coordinator and live owner must be verified independently.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from .bus_projection import AppendCheckpoint
from .bus_publication import (
    unique_wire_object,
)
from .bus_source_page import CandidateQuery
from .checkpoint_seals import PrefixSource
from .delivery_policy import KeyedResponseReceipt
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .typed_table import Column, Index, SQLiteSchemaObject, TypedStore, TypedTable
from .wake import NoWakeDecision, WakeDecision
from .wake_policy import WakePolicy
from .wire_log import OpenedWireSnapshot, WireLog
from .wire_metadata import WireRootIdText
from .wire_record import WireRecord


class ProjectionUnavailableError(RuntimeError):
    """Omit the optional supplement; original delivery remains unaffected."""


class ProjectionRebuildRequiredError(ProjectionUnavailableError):
    """A changed source or checkpoint needs explicit bounded maintenance."""


class CandidateTable:
    """Disposable projections: source identity, recipient hints, and duplicate response keys."""


@dataclass(frozen=True)
class CandidateCheckpoint(CandidateTable, TypedTable):
    singleton: Literal[1] = field(metadata={"sql": Column(primary_key=True)})
    version: Literal[3]
    root_id: str
    device: int
    inode: int
    byte_offset: int = field(metadata={"sql": Column(check="byte_offset>=0")})
    tail_digest: str
    last_seq: int = field(metadata={"sql": Column(check="last_seq>=0")})


    @classmethod
    def current(cls, db: sqlite3.Connection) -> CandidateCheckpoint | None:
        try:
            return cls.one(db, singleton=1)
        except (ValueError, TypeError) as error:
            raise ProjectionRebuildRequiredError("candidate index schema is unavailable") from error

    @property
    def source_identity(self) -> PrefixSource:
        return PrefixSource(self.root_id, self.device, self.inode)

    def require_source(self, stream, info: os.stat_result, root_id: str) -> None:
        if self.source_identity != PrefixSource(root_id, info.st_dev, info.st_ino):
            raise ProjectionRebuildRequiredError("candidate bus source changed")
        if self.byte_offset > info.st_size:
            raise ProjectionRebuildRequiredError("candidate bus prefix was truncated")
        if AppendCheckpoint.fingerprint(stream, self.byte_offset) != self.tail_digest:
            raise ProjectionRebuildRequiredError("candidate bus prefix changed")


@dataclass(frozen=True)
class CandidateResponseKey(CandidateTable, TypedTable):
    publication_key: str = field(metadata={"sql": Column(primary_key=True)})
    without_rowid = True


@dataclass(frozen=True)
class Candidate(CandidateTable, TypedTable):
    source_seq: int = field(metadata={"sql": Column(primary_key=True)})
    message_id: str
    recipient_lookup: str = field(metadata={"sql": Column(primary_key=True)})
    sender: str
    target: str
    wake_mode: type[WakePolicy] | None  # None means a delivery-only/no-wake member.
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
    """Untrusted scheduling hint, never a sealed claim or native input receipt."""

    root_id: Annotated[str, WireRootIdText]
    through_bytes: int

    def __post_init__(self) -> None:
        if self.through_bytes <= 0:
            raise ValueError("exact append hint requires a positive original byte position")

    @classmethod
    def capture(cls, root_id: str, through_bytes: int):
        return FieldCodec.decode(cls, {"root_id": root_id, "through_bytes": through_bytes})

    def validated(self):
        return FieldCodec.decode(type(self), FieldCodec.encode(self))


@dataclass(frozen=True, slots=True)
class CandidateCatchUp:
    """One bounded derived-index maintenance result, not delivery authority."""

    checkpoint_bytes: int
    caught_up: bool
    more_source_bytes: bool


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    seq: int
    recipients: tuple[Candidate, ...]
    response: KeyedResponseReceipt | None


@dataclass(frozen=True, slots=True)
class _ReplayBatch:
    next_offset: int
    last_seq: int
    recipients: tuple[Candidate, ...]
    response_keys: tuple[CandidateResponseKey, ...]


class WakeCandidateIndex(TypedStore):
    """Read-only candidate pages plus an explicit, bounded maintenance API.

    Never call ``maintain`` in the send or wake path. Each call only
    processes up to max_rows/max_bytes; rows are keyed by original bus identity
    and commit with the byte checkpoint in one SQLite transaction. Even an
    exact page is NOT evidence of a sealed claim or a model-accepted input.
    """

    batch_rows = 64
    batch_bytes = 256 * 1024
    batch_seconds = 0.2

    filename = "wake_candidates.sqlite3"
    rows = tuple(TypedTable.members_with(CandidateTable))

    def __init__(self, log: WireLog):
        if type(log) is not WireLog or log.path.name != "bus.jsonl":
            raise TypeError("candidate index requires the canonical wire log")
        self.log = log

    @property
    def root(self) -> Path:
        return self.log.path.parent

    def require_schema(self, actual: list[SQLiteSchemaObject]) -> None:
        try:
            super().require_schema(actual)
        except RelationViolationError as error:
            raise ProjectionRebuildRequiredError(
                "candidate index schema changed; reset required"
            ) from error

    @staticmethod
    def _validate_limits(max_rows: int, max_bytes: int) -> None:
        if type(max_rows) is not int or not 1 <= max_rows <= 256:
            raise ValueError("max_rows must be between 1 and 256")
        if type(max_bytes) is not int or not 1 <= max_bytes <= WireRecord.max_row_bytes:
            raise ValueError("max_bytes must be a bounded positive byte count")

    @staticmethod
    def _checkpoint_start(
        checkpoint: CandidateCheckpoint | None,
        stream: Any,
        stat: os.stat_result,
        root_id: str,
        *,
        rebuild: bool,
        bootstrap_new: bool = False,
    ) -> CandidateCheckpoint:
        """Derive replay coordinates from the one originally captured stored row."""
        if checkpoint is None and not (rebuild or bootstrap_new):
            raise ProjectionRebuildRequiredError("candidate index requires initial rebuild")
        if rebuild or checkpoint is None:
            return CandidateCheckpoint(1, 3, root_id, stat.st_dev, stat.st_ino, 0, "", 0)
        checkpoint.require_source(stream, stat, root_id)
        return checkpoint

    @staticmethod
    def _parse_row(record: dict[str, Any], root_id: str, last_seq: int) -> _ParsedRow:
        """Interpret one raw bus object without mistaking its sideband for authority."""
        from .wire_record import WireRecord

        verified = WireRecord.from_wire(record, root_id)
        sequence = verified.sequence_after(last_seq)
        rows: list[Candidate] = []
        for initial in verified.deliveries():
            message = initial.message
            for recipient, decision in zip(
                initial.audience.recipients, initial.decisions, strict=True
            ):
                if type(decision) not in {WakeDecision, NoWakeDecision}:
                    raise ProjectionUnavailableError("unknown candidate wake decision")
                rows.append(
                    Candidate(
                        message.seq,
                        message.message_id,
                        recipient.recipient_lookup,
                        message.sender,
                        message.target,
                        type(decision.wake_mode)
                        if type(decision) is WakeDecision
                        else None,
                    )
                )
        return _ParsedRow(sequence, tuple(rows), verified.receipt)

    @classmethod
    def _replay_prefix(
        cls,
        source: OpenedWireSnapshot,
        offset: int,
        last_seq: int,
        *,
        max_rows: int,
        max_bytes: int,
    ) -> _ReplayBatch:
        """Replay a bounded batch inside the original committed source cut."""
        stream = source.stream
        rows: list[Candidate] = []
        response_keys: list[CandidateResponseKey] = []
        stream.seek(offset)
        start = time.monotonic()
        admitted = 0
        for _ in range(max_rows):
            if time.monotonic() - start > cls.batch_seconds:
                break  # Bounded maintenance; no expensive hot-path rebuild.
            available = max_bytes - (stream.tell() - offset)
            if available <= 0:
                break
            remaining = source.boundary - stream.tell()
            if remaining <= 0:
                break
            raw = stream.readline(min(WireRecord.max_row_bytes + 1, remaining))
            if not raw:
                break
            WireRecord.require_row_size(raw, error_type=ProjectionUnavailableError)
            if not raw.endswith(b"\n"):
                raise ProjectionUnavailableError("incomplete candidate bus row")
            if len(raw) > available and admitted:
                # Keep the batch boundary inside the byte budget once it has
                # admitted at least one record.
                stream.seek(-(len(raw)), os.SEEK_CUR)
                break
            # A first record larger than the batch budget is admitted alone:
            # every batch with remaining source bytes must advance the
            # checkpoint; WireRecord independently admits its physical size.
            admitted += 1
            record = json.loads(raw, object_pairs_hook=unique_wire_object)
            if not isinstance(record, dict):
                raise ProjectionUnavailableError("candidate bus row is not an object")
            parsed = cls._parse_row(record, source.metadata.root_id, last_seq)
            last_seq = parsed.seq
            rows.extend(parsed.recipients)
            if parsed.response is not None:
                response_keys.append(CandidateResponseKey(parsed.response.publication_key))
        return _ReplayBatch(stream.tell(), last_seq, tuple(rows), tuple(response_keys))

    def _source_end(self, stream: Any, next_offset: int) -> str:
        """Reject path replacement before committing an index checkpoint."""
        after = os.fstat(stream.fileno())
        current_path = self.log.path.stat()
        if (after.st_dev, after.st_ino) != (current_path.st_dev, current_path.st_ino):
            raise ProjectionRebuildRequiredError("candidate bus was replaced")
        return AppendCheckpoint.fingerprint(stream, next_offset)

    @staticmethod
    def _commit_batch(
        db: sqlite3.Connection,
        batch: _ReplayBatch,
        stat: os.stat_result,
        root_id: str,
        tail: str,
        *,
        rebuild: bool,
    ) -> None:
        """Replace derived rows and checkpoint inside the caller's transaction."""
        if rebuild:
            db.execute(f'DELETE FROM "{Candidate.declared_name}"')
            db.execute(f'DELETE FROM "{CandidateResponseKey.declared_name}"')
        for row in batch.recipients:
            row.insert(db)
        for row in batch.response_keys:
            if CandidateResponseKey.one(db, publication_key=row.publication_key) is not None:
                raise ProjectionUnavailableError("duplicate private response publication key")
            row.insert(db)
        CandidateCheckpoint(
            1,
            3,
            root_id,
            stat.st_dev,
            stat.st_ino,
            batch.next_offset,
            tail,
            batch.last_seq,
        ).upsert(db)

    def maintain(
        self, *, rebuild: bool = False, bootstrap_new: bool = False,
        max_rows: int = batch_rows, max_bytes: int = batch_bytes,
    ) -> bool:
        """Replay one bounded prefix; never scan or rebuild on the send/wake path."""
        self._validate_limits(max_rows, max_bytes)
        bootstrap_new = FieldCodec.decode(bool, bootstrap_new)
        try:
            with self.log._opened_wire_snapshot(need_sequence=False) as source:
                stream = source.stream
                if stream is None:
                    raise ProjectionUnavailableError("candidate bus source is missing")
                root_id = source.metadata.root_id
                stat = os.fstat(stream.fileno())
                with self.writing() as db:
                    # TypedStore installs schema atomically. Capture the stored
                    # row, then release SQL custody before reading or decoding.
                    recorded = CandidateCheckpoint.current(db)
                checkpoint = self._checkpoint_start(
                    recorded, stream, stat, root_id,
                    rebuild=rebuild, bootstrap_new=bootstrap_new,
                )
                batch = self._replay_prefix(
                    source, checkpoint.byte_offset, checkpoint.last_seq,
                    max_rows=max_rows, max_bytes=max_bytes,
                )
                with self.writing() as db:
                    current = CandidateCheckpoint.current(db)
                    if current == recorded:
                        tail = self._source_end(stream, batch.next_offset)
                        self._commit_batch(db, batch, stat, root_id, tail, rebuild=rebuild)
                        return batch.next_offset >= source.boundary
                # Another writer's committed row owns progress. Release SQL
                # custody before validating its source; never replay a batch.
                advanced = self._checkpoint_start(
                    current, stream, os.fstat(stream.fileno()), root_id,
                    rebuild=False,
                )
                return advanced.byte_offset >= source.boundary
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            OverflowError,
            AttributeError,
            RelationViolationError,
            sqlite3.DatabaseError,
        ) as error:
            if isinstance(error, ProjectionUnavailableError):
                raise
            raise ProjectionUnavailableError("candidate index maintenance unavailable") from error

    def notify_committed_append(self, *, root_id: str, through_bytes: int) -> CommittedAppendHint:
        """Create a post-commit hint without touching the bus or WAL.

        The publisher must call this only AFTER a durable private bus commit and
        release its wire/bus locks. Hand the returned hint to a separate bounded
        maintenance task; calling ``catch_up_committed_append`` inline in the
        publication critical path would delay original delivery. Hints are
        volatile: losing one in a crash leaves the index stale/unavailable, and
        a later hint or explicit maintenance can catch up from the checkpoint.
        Byte positions also cover observations which do not allocate sequences.
        No hint proves that its claimed source prefix actually committed.
        """
        return CommittedAppendHint.capture(root_id, through_bytes)


    def _verified_checkpoint(self, root_id: str) -> CandidateCheckpoint:
        """Check one WAL checkpoint row against its bounded source prefix-tail witness."""
        try:
            marker = self.log._private_marker_unlocked()
            if marker.root_id != root_id:
                raise ProjectionRebuildRequiredError("candidate private root changed")
            with self.snapshot() as db:
                with self.log.path.open("rb") as stream:
                    stat = os.fstat(stream.fileno())
                    checkpoint = self._checkpoint_start(
                        CandidateCheckpoint.current(db),
                        stream, stat, root_id, rebuild=False,
                    )
                return checkpoint
        except (OSError, RelationViolationError, sqlite3.DatabaseError) as error:
            raise ProjectionUnavailableError("candidate checkpoint unavailable") from error

    def catch_up_committed_append(
        self,
        hint: CommittedAppendHint,
        *,
        max_rows: int = batch_rows,
        max_bytes: int = batch_bytes,
        bootstrap_new: bool = False,
    ) -> CandidateCatchUp:
        """Replay at most ONE bounded batch after a durable append notification.

        Run outside publisher locks and off its latency path. An absent
        derived index may be explicitly bootstrapped in bounded batches; an
        mismatched schema, missing checkpoint, changed/truncated source or corrupt
        WAL never gets an implicit rebuild. If ``caught_up`` is false and
        ``more_source_bytes`` is true, a deferred worker may schedule another
        finite batch. Pages remain unavailable for required high-water until
        the index catches up. This
        contains no seal, owner, claim or native proof and never retries work.
        """
        if type(hint) is not CommittedAppendHint:
            raise ValueError("candidate catch-up requires the original append hint")
        hint = hint.validated()
        bootstrap_new = FieldCodec.decode(bool, bootstrap_new)
        self._validate_limits(max_rows, max_bytes)
        if self.log._private_marker_unlocked().root_id != hint.root_id:
            raise ProjectionRebuildRequiredError("candidate append hint belongs to another root")
        prior_bytes = 0
        if bootstrap_new:
            # Existence is not initialization: another process may have created
            # the WAL but not committed its first checkpoint. BEGIN IMMEDIATE in
            # maintain serializes that decision against the stored row.
            more_source_bytes = not self.maintain(
                bootstrap_new=True, max_rows=max_rows, max_bytes=max_bytes
            )
        else:
            if not self.path.exists():
                raise ProjectionRebuildRequiredError(
                    "candidate index needs explicit initial build"
                )
            prior = self._verified_checkpoint(hint.root_id)
            prior_bytes = prior.byte_offset
            if prior.byte_offset >= hint.through_bytes:
                return CandidateCatchUp(prior.byte_offset, True, False)
            more_source_bytes = not self.maintain(
                max_rows=max_rows, max_bytes=max_bytes
            )
        checkpoint = self._verified_checkpoint(hint.root_id)
        if checkpoint.byte_offset <= prior_bytes and more_source_bytes:
            # The checkpoint row owns the byte position: a batch that consumed
            # no source bytes while the source has more is the only true
            # no-progress state; observation rows can advance bytes without
            # moving last_seq, which is progress, not failure.
            raise ProjectionUnavailableError(
                "candidate batch consumed no source bytes while source bytes remain"
            )
        if checkpoint.byte_offset < hint.through_bytes and not more_source_bytes:
            raise ProjectionUnavailableError(
                "candidate notification exceeds verified source bytes"
            )
        return CandidateCatchUp(
            checkpoint.byte_offset, checkpoint.byte_offset >= hint.through_bytes, more_source_bytes
        )

    def page(
        self,
        *,
        root_id: str,
        recipient_lookup: str,
        after_seq: int,
        required_through_seq: int,
        limit: int = 32,
        delivery_only: bool = False,
    ) -> CandidatePage:
        """Read only a bounded WAL page; caller MUST verify bus+SQL+owner.

        Root and high-water must come from the caller's trusted snapshot, never
        agent/model arguments. Returning a page does not attest current state.
        """
        request = CandidateQuery.capture(root_id=root_id, recipient_lookup=recipient_lookup,
                     after_seq=after_seq, required_through_seq=required_through_seq,
                     limit=limit, delivery_only=delivery_only)
        try:
            with self.snapshot() as db:
                checkpoint = CandidateCheckpoint.current(db)
                if (
                    checkpoint is None
                    or checkpoint.root_id != request.root_id
                    or checkpoint.last_seq < request.required_through_seq
                ):
                    raise ProjectionUnavailableError("candidate projection is stale or unrelated")
                with self.log.path.open("rb") as stream:
                    stat = os.fstat(stream.fileno())
                    checkpoint.require_source(stream, stat, request.root_id)
                data = Candidate.read(
                    db.execute(
                        f'SELECT * FROM "{Candidate.declared_name}" '
                        "WHERE recipient_lookup=? AND source_seq>? "
                        + (
                            "AND wake_mode IS NULL "
                            if request.delivery_only
                            else "AND wake_mode IS NOT NULL "
                        )
                        + "ORDER BY source_seq LIMIT ?",
                        (request.recipient_lookup, request.after_seq, request.limit + 1),
                    )
                )
                return CandidatePage(
                    checkpoint.last_seq,
                    tuple(data[:request.limit]),
                    len(data) > request.limit,
                )
        except (OSError, sqlite3.DatabaseError, ValueError, TypeError) as error:
            raise ProjectionUnavailableError("candidate projection read unavailable") from error
