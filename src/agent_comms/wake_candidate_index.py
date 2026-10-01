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
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from .bus_publication import (
    unique_wire_object,
)
from .delivery_policy import KeyedResponseReceipt
from .bus_source_page import CandidateQuery
from .bus_projection import AppendCheckpoint
from .checkpoint_seals import PrefixSource
from .field_codec import FieldCodec
from .wire_metadata import WireRootIdText
from .errors import RelationViolationError
from .message_bus import MessageBus
from .typed_table import Column, Index, SQLiteJournalMode, SQLiteSchemaObject, TypedTable
from .wake import NoWakeDecision, WakeDecision

_MAX_ROW = 8 * 1024 * 1024
_TIMEOUT = 0.05  # Busy readers/writers must not stall a wake for seconds.


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
    wake_mode: str | None  # None means a delivery-only/no-wake member.
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
    through_seq: int

    def __post_init__(self) -> None:
        if self.through_seq <= 0:
            raise ValueError("exact append hint requires a positive original sequence")

    @classmethod
    def capture(cls, root_id: str, through_seq: int):
        return FieldCodec.decode(cls, {"root_id": root_id, "through_seq": through_seq})

    def validated(self):
        return FieldCodec.decode(type(self), FieldCodec.encode(self))


@dataclass(frozen=True, slots=True)
class CandidateCatchUp:
    """One bounded derived-index maintenance result, not delivery authority."""

    checkpoint_seq: int
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


class WakeCandidateIndex:
    """Read-only candidate pages plus an explicit, bounded maintenance API.

    Never call ``maintain`` in the send or wake path. Each call only
    processes up to max_rows/max_bytes; rows are keyed by original bus identity
    and commit with the byte checkpoint in one SQLite transaction. Even an
    exact page is NOT evidence of a sealed claim or a model-accepted input.
    """

    def __init__(self, bus: MessageBus):
        if type(bus) is not MessageBus or bus.log.path.name != "bus.jsonl":
            raise TypeError("candidate index requires the canonical bus")
        self.bus = bus
        self.path = bus.log.path.with_name("wake_candidates.sqlite3")

    @staticmethod
    def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
        if readonly:
            db = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=_TIMEOUT)
            db.execute("PRAGMA query_only=ON")
        else:
            db = sqlite3.connect(path, timeout=_TIMEOUT)
            if SQLiteJournalMode.read(db.execute("PRAGMA journal_mode=WAL")) != [
                SQLiteJournalMode("wal")
            ]:
                db.close()
                raise ProjectionUnavailableError("candidate index requires WAL")
            db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=50")
        return db

    @staticmethod
    def _schema(db: sqlite3.Connection, *, create: bool) -> None:
        schema = {
            name: sql
            for table in TypedTable.members_with(CandidateTable)
            for name, sql in table.schema_objects().items()
        }
        try:
            actual = SQLiteSchemaObject.read(
                db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")
            )
            if not actual and create:
                with db:
                    for table in TypedTable.members_with(CandidateTable):
                        table.create(db)
                return
            if {row.name: row.sql for row in actual} != schema:
                raise ProjectionRebuildRequiredError(
                    "candidate index schema changed; reset required"
                )
            CandidateCheckpoint.one(db, singleton=1)
        except (sqlite3.DatabaseError, ValueError, TypeError) as error:
            raise ProjectionRebuildRequiredError("candidate index schema is unavailable") from error

    @staticmethod
    def _validate_limits(max_rows: int, max_bytes: int) -> None:
        if type(max_rows) is not int or not 1 <= max_rows <= 256:
            raise ValueError("max_rows must be between 1 and 256")
        if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_ROW:
            raise ValueError("max_bytes must be a bounded positive byte count")

    @classmethod
    def _checkpoint_start(
        cls,
        db: sqlite3.Connection,
        stream: Any,
        stat: os.stat_result,
        root_id: str,
        *,
        rebuild: bool,
    ) -> tuple[int, int]:
        """Check the committed source identity and its bounded prefix fingerprint."""
        checkpoint = CandidateCheckpoint.one(db, singleton=1)
        if checkpoint is None and not rebuild:
            raise ProjectionRebuildRequiredError("candidate index requires initial rebuild")
        if rebuild:
            return 0, 0
        checkpoint.require_source(stream, stat, root_id)
        return checkpoint.byte_offset, checkpoint.last_seq

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
                        decision.wake_mode.declared_name
                        if type(decision) is WakeDecision
                        else None,
                    )
                )
        return _ParsedRow(sequence, tuple(rows), verified.receipt)

    @classmethod
    def _replay_prefix(
        cls,
        stream: Any,
        offset: int,
        last_seq: int,
        root_id: str,
        *,
        max_rows: int,
        max_bytes: int,
    ) -> _ReplayBatch:
        """Read only a bounded complete prefix; yield parsed receipts to SQL."""
        rows: list[Candidate] = []
        response_keys: list[CandidateResponseKey] = []
        stream.seek(offset)
        start = time.monotonic()
        for _ in range(max_rows):
            if time.monotonic() - start > 0.2:
                break  # Bounded maintenance; no expensive hot-path rebuild.
            available = max_bytes - (stream.tell() - offset)
            if available <= 0:
                break
            raw = stream.readline(min(available, _MAX_ROW) + 1)
            if not raw:
                break
            if len(raw) > available:
                stream.seek(-(len(raw)), os.SEEK_CUR)
                break
            if not raw.endswith(b"\n"):
                raise ProjectionUnavailableError("incomplete candidate bus row")
            record = json.loads(raw, object_pairs_hook=unique_wire_object)
            if not isinstance(record, dict):
                raise ProjectionUnavailableError("candidate bus row is not an object")
            parsed = cls._parse_row(record, root_id, last_seq)
            last_seq = parsed.seq
            rows.extend(parsed.recipients)
            if parsed.response is not None:
                response_keys.append(CandidateResponseKey(parsed.response.publication_key))
        return _ReplayBatch(stream.tell(), last_seq, tuple(rows), tuple(response_keys))

    def _source_end(self, stream: Any, next_offset: int) -> tuple[os.stat_result, str]:
        """Reject path replacement before committing an index checkpoint."""
        after = os.fstat(stream.fileno())
        current_path = self.bus.log.path.stat()
        if (after.st_dev, after.st_ino) != (current_path.st_dev, current_path.st_ino):
            raise ProjectionRebuildRequiredError("candidate bus was replaced")
        return after, AppendCheckpoint.fingerprint(stream, next_offset)

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
        """Replace derived rows and checkpoint together, or expose neither."""
        with db:
            if rebuild:
                db.execute(f'DELETE FROM "{Candidate.declared_name}"')
                db.execute(f'DELETE FROM "{CandidateResponseKey.declared_name}"')
            for row in batch.recipients:
                row.insert(db)
            try:
                for row in batch.response_keys:
                    row.insert(db)
            except sqlite3.IntegrityError as error:
                raise ProjectionUnavailableError(
                    "duplicate private response publication key"
                ) from error
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
        self, *, rebuild: bool = False, max_rows: int = 64, max_bytes: int = 256 * 1024
    ) -> bool:
        """Replay one bounded prefix; never scan or rebuild on the send/wake path."""
        self._validate_limits(max_rows, max_bytes)
        try:
            marker = self.bus.log._private_marker_unlocked()
            root_id = marker.root_id
            with self.bus.log.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                with closing(self._connect(self.path, readonly=False)) as db:
                    # Rebuild replays current-format projections only; old schemas
                    # require the explicit store reset outside runtime code.
                    self._schema(db, create=True)
                    offset, last_seq = self._checkpoint_start(
                        db, stream, stat, root_id, rebuild=rebuild
                    )
                    batch = self._replay_prefix(
                        stream,
                        offset,
                        last_seq,
                        root_id,
                        max_rows=max_rows,
                        max_bytes=max_bytes,
                    )
                    after, tail = self._source_end(stream, batch.next_offset)
                    self._commit_batch(db, batch, stat, root_id, tail, rebuild=rebuild)
                    return batch.next_offset == after.st_size
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

    def notify_committed_append(self, *, root_id: str, through_seq: int) -> CommittedAppendHint:
        """Create a post-commit hint without touching the bus or WAL.

        The publisher must call this only AFTER a durable private bus commit and
        release its wire/bus locks. Hand the returned hint to a separate bounded
        maintenance task; calling ``catch_up_committed_append`` inline in the
        publication critical path would delay original delivery. Hints are
        volatile: losing one in a crash leaves the index stale/unavailable, and
        a later hint or explicit maintenance can catch up from the checkpoint.
        No hint proves that its claimed sequence actually committed.
        """
        return CommittedAppendHint.capture(root_id, through_seq)


    def _verified_checkpoint(self, root_id: str) -> int:
        """Check one WAL checkpoint against its bounded source prefix-tail witness."""
        try:
            marker = self.bus.log._private_marker_unlocked()
            if marker.root_id != root_id:
                raise ProjectionRebuildRequiredError("candidate private root changed")
            with closing(self._connect(self.path, readonly=True)) as db:
                self._schema(db, create=False)
                db.execute("BEGIN")
                with self.bus.log.path.open("rb") as stream:
                    stat = os.fstat(stream.fileno())
                    offset, last_seq = self._checkpoint_start(
                        db, stream, stat, root_id, rebuild=False
                    )
                    self._source_end(stream, offset)
                    # _checkpoint_start verifies the saved prefix; the source
                    # may have newer complete rows not yet in this projection.
                    if stat.st_size:
                        stream.seek(-1, os.SEEK_END)
                        if stream.read(1) != b"\n":
                            raise ProjectionUnavailableError(
                                "candidate source has an incomplete tail"
                            )
                return last_seq
        except (OSError, RelationViolationError, sqlite3.DatabaseError) as error:
            raise ProjectionUnavailableError("candidate checkpoint unavailable") from error

    def catch_up_committed_append(
        self,
        hint: CommittedAppendHint,
        *,
        max_rows: int = 64,
        max_bytes: int = 256 * 1024,
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
        if self.bus.log._private_marker_unlocked().root_id != hint.root_id:
            raise ProjectionRebuildRequiredError("candidate append hint belongs to another root")
        if self.path.exists():
            prior = self._verified_checkpoint(hint.root_id)
            if prior >= hint.through_seq:
                return CandidateCatchUp(prior, True, False)
            rebuild = False
        elif bootstrap_new:
            prior = 0
            rebuild = True
        else:
            raise ProjectionRebuildRequiredError("candidate index needs explicit initial build")
        more_source_bytes = not self.maintain(
            rebuild=rebuild, max_rows=max_rows, max_bytes=max_bytes
        )
        checkpoint = self._verified_checkpoint(hint.root_id)
        if checkpoint <= prior and more_source_bytes:
            raise ProjectionUnavailableError(
                "candidate batch made no checkpoint progress; explicit larger budget required"
            )
        if checkpoint < hint.through_seq and not more_source_bytes:
            raise ProjectionUnavailableError(
                "candidate notification exceeds verified bus high-water"
            )
        return CandidateCatchUp(checkpoint, checkpoint >= hint.through_seq, more_source_bytes)

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
            with closing(self._connect(self.path, readonly=True)) as db:
                self._schema(db, create=False)
                db.execute("BEGIN")
                checkpoint = CandidateCheckpoint.one(db, singleton=1)
                if (
                    checkpoint is None
                    or checkpoint.root_id != request.root_id
                    or checkpoint.last_seq < request.required_through_seq
                ):
                    raise ProjectionUnavailableError("candidate projection is stale or unrelated")
                with self.bus.log.path.open("rb") as stream:
                    stat = os.fstat(stream.fileno())
                    if stat.st_size:
                        stream.seek(-1, os.SEEK_END)
                        if stream.read(1) != b"\n":
                            raise ProjectionUnavailableError(
                                "candidate source has an incomplete tail"
                            )
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
