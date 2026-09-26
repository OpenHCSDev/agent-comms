"""Disposable WAL index of private N/K *candidates*, never wake authority.

Maintenance explicitly replays a bounded bus prefix; a page reads SQLite WAL
plus a 4 KiB prefix-tail fingerprint, never the whole wire or a bus lock.
Pages never claim that bus decisions are sealed coordinator receipts.
The original bus, coordinator and live owner must be verified independently.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bus_publication import (
    PRIVATE_WIRE_FIELD,
    _canonical,
    has_private_wire_fields,
    public_envelope_digest,
    unique_wire_object,
    validate_initial_record,
)
from .coordination import CoordinationError, PublicationReceipt
from .declarations import Message, MessageBus, RelationViolationError
from .wake import NoWakeDecision, WakeDecision

_SCHEMA = 2
_MAX_ROW = 8 * 1024 * 1024
_TIMEOUT = 0.05  # Busy readers/writers must not stall a wake for seconds.


class ProjectionUnavailableError(RuntimeError):
    """Omit the optional supplement; original delivery remains unaffected."""


class ProjectionRebuildRequiredError(ProjectionUnavailableError):
    """A changed source or checkpoint needs explicit bounded maintenance."""


@dataclass(frozen=True, slots=True)
class Candidate:
    source_seq: int
    message_id: str
    recipient_lookup: str
    sender: str
    target: str
    wake_mode: str | None  # None means a delivery-only/no-wake member.


@dataclass(frozen=True, slots=True)
class CandidatePage:
    through_seq: int
    entries: tuple[Candidate, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class _IndexedResponse:
    """The existing typed receipt plus its two private-bus-only fields."""

    receipt: PublicationReceipt
    wire_root_id: str
    envelope_digest: str


_RecipientRow = tuple[int, str, str, str, str, str | None]


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    seq: int
    recipients: tuple[_RecipientRow, ...]
    response: _IndexedResponse | None


@dataclass(frozen=True, slots=True)
class _ReplayBatch:
    next_offset: int
    last_seq: int
    recipients: tuple[_RecipientRow, ...]
    response_keys: tuple[tuple[str], ...]


def _parse_response(message: Message, private: dict[str, Any]) -> _IndexedResponse:
    raw = private["response"]
    if not isinstance(raw, dict) or set(raw) != {
        "wire_root_id",
        "execution_id",
        "publication_key",
        "envelope_digest",
    }:
        raise ProjectionUnavailableError("malformed candidate response")
    try:
        receipt = PublicationReceipt(
            raw["execution_id"],
            raw["publication_key"],
            message.seq,
            message.message_id,
            message.sender,
            message.target,
            message.type,
            message.notice,
            message.timestamp,
            hashlib.sha256(message.body.encode("utf-8")).hexdigest(),
        )
    except (CoordinationError, TypeError, ValueError, UnicodeError) as error:
        raise ProjectionUnavailableError("malformed candidate response receipt") from error
    return _IndexedResponse(receipt, raw["wire_root_id"], raw["envelope_digest"])


class WakeCandidateIndex:
    """Read-only candidate pages plus an explicit, bounded maintenance API.

    Never call ``maintain`` in the send or wake path. Each call only
    processes up to max_rows/max_bytes; rows are keyed by original bus identity
    and commit with the byte checkpoint in one SQLite transaction. Even an
    exact page is NOT evidence of a sealed claim or a model-accepted input.
    """

    def __init__(self, bus: MessageBus):
        if type(bus) is not MessageBus or bus._path.name != "bus.jsonl":
            raise TypeError("candidate index requires the canonical bus")
        self.bus = bus
        self.path = bus._path.with_name("wake_candidates.sqlite3")

    @staticmethod
    def _tail(stream: Any, offset: int) -> str:
        start = max(0, offset - 4096)
        stream.seek(start)
        return hashlib.sha256(stream.read(offset - start)).hexdigest()

    @staticmethod
    def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
        if readonly:
            db = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=_TIMEOUT)
            db.execute("PRAGMA query_only=ON")
        else:
            db = sqlite3.connect(path, timeout=_TIMEOUT)
            if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                db.close()
                raise ProjectionUnavailableError("candidate index requires WAL")
            db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=50")
        return db

    @staticmethod
    def _schema(db: sqlite3.Connection, *, create: bool, allow_v1_rebuild: bool = False) -> None:
        if create:
            db.execute(
                "CREATE TABLE IF NOT EXISTS checkpoint ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                "version INTEGER NOT NULL,root_id TEXT NOT NULL,device INTEGER NOT NULL,"
                "inode INTEGER NOT NULL,byte_offset INTEGER NOT NULL,"
                "tail_digest TEXT NOT NULL,last_seq INTEGER NOT NULL) STRICT"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS recipients ("
                "source_seq INTEGER NOT NULL,message_id TEXT NOT NULL,"
                "recipient_lookup TEXT NOT NULL,sender TEXT NOT NULL,target TEXT NOT NULL,"
                "wake_mode TEXT,PRIMARY KEY(source_seq,recipient_lookup)) WITHOUT ROWID"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS selected_recipient_seq "
                "ON recipients(recipient_lookup,source_seq) WHERE wake_mode IS NOT NULL"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS passive_recipient_seq "
                "ON recipients(recipient_lookup,source_seq) WHERE wake_mode IS NULL"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS response_keys ("
                "publication_key TEXT PRIMARY KEY) WITHOUT ROWID"
            )
        try:
            version = db.execute("SELECT version FROM checkpoint WHERE singleton=1").fetchone()
        except sqlite3.DatabaseError as error:
            raise ProjectionRebuildRequiredError("candidate index schema is unavailable") from error
        if (
            version is not None
            and version[0] != _SCHEMA
            and not (create and allow_v1_rebuild and version[0] == 1)
        ):
            raise ProjectionRebuildRequiredError("candidate index schema version changed")

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
        checkpoint = db.execute(
            "SELECT root_id,device,inode,byte_offset,tail_digest,last_seq "
            "FROM checkpoint WHERE singleton=1"
        ).fetchone()
        if checkpoint is None and not rebuild:
            raise ProjectionRebuildRequiredError("candidate index requires initial rebuild")
        if rebuild:
            return 0, 0
        saved_root, dev, ino, offset, tail, last_seq = checkpoint
        if (
            saved_root != root_id
            or dev != stat.st_dev
            or ino != stat.st_ino
            or type(offset) is not int
            or offset < 0
            or offset > stat.st_size
            or type(last_seq) is not int
            or last_seq < 0
            or type(tail) is not str
            or cls._tail(stream, offset) != tail
        ):
            raise ProjectionRebuildRequiredError("candidate bus prefix changed")
        return offset, last_seq

    @staticmethod
    def _parse_row(record: dict[str, Any], root_id: str, last_seq: int) -> _ParsedRow:
        """Interpret one raw bus object without mistaking its sideband for authority."""
        message = Message.from_wire(record)
        public = {key: value for key, value in record.items() if key != PRIVATE_WIRE_FIELD}
        if (
            type(record.get("seq")) is not int
            or message.seq <= last_seq
            or _canonical(public) != _canonical(message.to_wire())
        ):
            raise ProjectionUnavailableError("candidate bus envelope is not canonical")
        envelope_digest = public_envelope_digest(public)
        private = record.get(PRIVATE_WIRE_FIELD)
        if has_private_wire_fields(record):
            if set(key for key in record if key.startswith("_agent_comms_private")) != {
                PRIVATE_WIRE_FIELD
            }:
                raise ProjectionUnavailableError("unknown candidate private namespace")
            if (
                not isinstance(private, dict)
                or type(private.get("version")) is not int
                or private["version"] != 1
            ):
                raise ProjectionUnavailableError("unknown candidate private row")
        if isinstance(private, dict) and set(private) == {"version", "initial"}:
            initial = validate_initial_record(record, root_id)
            rows: list[_RecipientRow] = []
            for recipient, decision in zip(
                initial.audience.recipients, initial.decisions, strict=True
            ):
                if type(decision) not in {WakeDecision, NoWakeDecision}:
                    raise ProjectionUnavailableError("unknown candidate wake decision")
                rows.append(
                    (
                        message.seq,
                        message.message_id,
                        recipient.recipient_lookup,
                        message.sender,
                        message.target,
                        decision.wake_mode.value if type(decision) is WakeDecision else None,
                    )
                )
            return _ParsedRow(message.seq, tuple(rows), None)
        if private is not None:
            if set(private) != {"version", "response"}:
                raise ProjectionUnavailableError("unknown candidate private row")
            response = _parse_response(message, private)
            if response.wire_root_id != root_id:
                raise ProjectionUnavailableError("foreign private response root")
            if response.envelope_digest != envelope_digest:
                raise ProjectionUnavailableError("private response envelope mismatch")
            return _ParsedRow(message.seq, (), response)
        return _ParsedRow(message.seq, (), None)

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
        rows: list[_RecipientRow] = []
        response_keys: list[tuple[str]] = []
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
                response_keys.append((parsed.response.receipt.publication_key,))
        return _ReplayBatch(stream.tell(), last_seq, tuple(rows), tuple(response_keys))

    def _source_end(self, stream: Any, next_offset: int) -> tuple[os.stat_result, str]:
        """Reject path replacement before committing an index checkpoint."""
        after = os.fstat(stream.fileno())
        current_path = self.bus._path.stat()
        if (after.st_dev, after.st_ino) != (current_path.st_dev, current_path.st_ino):
            raise ProjectionRebuildRequiredError("candidate bus was replaced")
        return after, self._tail(stream, next_offset)

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
                db.execute("DELETE FROM recipients")
                db.execute("DELETE FROM response_keys")
            db.executemany("INSERT INTO recipients VALUES (?,?,?,?,?,?)", batch.recipients)
            try:
                db.executemany("INSERT INTO response_keys VALUES (?)", batch.response_keys)
            except sqlite3.IntegrityError as error:
                raise ProjectionUnavailableError(
                    "duplicate private response publication key"
                ) from error
            db.execute(
                "INSERT OR REPLACE INTO checkpoint VALUES (1,?,?,?,?,?,?,?)",
                (
                    _SCHEMA,
                    root_id,
                    stat.st_dev,
                    stat.st_ino,
                    batch.next_offset,
                    tail,
                    batch.last_seq,
                ),
            )

    def maintain(
        self, *, rebuild: bool = False, max_rows: int = 64, max_bytes: int = 256 * 1024
    ) -> bool:
        """Replay one bounded prefix; never scan or rebuild on the send/wake path."""
        self._validate_limits(max_rows, max_bytes)
        try:
            marker = self.bus._private_marker_unlocked()
            root_id = str(marker["wire_root_id"])
            with self.bus._path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                with closing(self._connect(self.path, readonly=False)) as db:
                    # Only explicit bounded rebuild may discard old v1 derived
                    # state; v2 and its response keys commit together.
                    self._schema(db, create=True, allow_v1_rebuild=rebuild)
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
        if (
            type(root_id) is not str
            or type(recipient_lookup) is not str
            or not recipient_lookup
            or type(after_seq) is not int
            or after_seq < 0
            or type(required_through_seq) is not int
            or required_through_seq < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
            or type(delivery_only) is not bool
        ):
            raise ValueError("candidate page requires bounded trusted inputs")
        try:
            with closing(self._connect(self.path, readonly=True)) as db:
                self._schema(db, create=False)
                db.execute("BEGIN")
                checkpoint = db.execute(
                    "SELECT root_id,device,inode,byte_offset,tail_digest,last_seq "
                    "FROM checkpoint WHERE singleton=1"
                ).fetchone()
                if (
                    checkpoint is None
                    or checkpoint[0] != root_id
                    or checkpoint[5] < required_through_seq
                ):
                    raise ProjectionUnavailableError("candidate projection is stale or unrelated")
                with self.bus._path.open("rb") as stream:
                    stat = os.fstat(stream.fileno())
                    if stat.st_size:
                        stream.seek(-1, os.SEEK_END)
                        if stream.read(1) != b"\n":
                            raise ProjectionUnavailableError(
                                "candidate source has an incomplete tail"
                            )
                    if (
                        (stat.st_dev, stat.st_ino) != tuple(checkpoint[1:3])
                        or stat.st_size < checkpoint[3]
                        or self._tail(stream, checkpoint[3]) != checkpoint[4]
                    ):
                        raise ProjectionRebuildRequiredError(
                            "candidate source changed; omit supplement"
                        )
                data = db.execute(
                    "SELECT source_seq,message_id,recipient_lookup,sender,target,wake_mode "
                    "FROM recipients WHERE recipient_lookup=? AND source_seq>? "
                    + ("AND wake_mode IS NULL " if delivery_only else "AND wake_mode IS NOT NULL ")
                    + "ORDER BY source_seq LIMIT ?",
                    (recipient_lookup, after_seq, limit + 1),
                ).fetchall()
                return CandidatePage(
                    checkpoint[5],
                    tuple(Candidate(*row) for row in data[:limit]),
                    len(data) > limit,
                )
        except (OSError, sqlite3.DatabaseError) as error:
            raise ProjectionUnavailableError("candidate projection read unavailable") from error
