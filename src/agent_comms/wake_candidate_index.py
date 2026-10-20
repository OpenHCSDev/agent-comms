"""Disposable WAL index of private N/K *candidates*, never wake authority.

Maintenance explicitly replays a bounded bus prefix; a page reads SQLite WAL
plus a 4 KiB prefix-tail fingerprint, never the whole wire or a bus lock.
Pages never claim that bus decisions are sealed coordinator evidences.
The original bus, coordinator and live owner must be verified independently.
"""

from __future__ import annotations

import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Any, Literal

from .bus_source_page import CandidateQuery
from .checkpoint_seals import PrefixSource
from .delivery_policy import KeyedResponseAcknowledgement
from .errors import RelationViolationError
from .field_codec import FieldCodec, Id128Text, MalformedRecord
from .private_path import FileRevision
from .typed_table import Column, Index, SQLiteSchemaObject, TypedStore, TypedTable
from .wake_policy import WakePolicy
from .wire_log import CurrentSnapshotOpening, SavedPrefixOpening, WireSnapshot, WireLog
from .wire_record import WireRecord


class ProjectionUnavailableError(RuntimeError):
    """Omit the optional supplement; original delivery remains unaffected."""


class ProjectionRebuildRequiredError(ProjectionUnavailableError):
    """A changed source or checkpoint needs explicit bounded maintenance."""


class CheckpointState(ABC):
    byte_offset: int

    @abstractmethod
    def require_source(self, source: WireSnapshot, root_id: str) -> CheckpointState: ...

    def start(self) -> IndexStart:
        return RebuildIndex()

    def checkpoint(self, source: WireSnapshot, root_id: str) -> CandidateCheckpoint:
        return CandidateCheckpoint.initial(root_id, source.revision)

    def refreshed(self, index: WakeCandidateIndex, root_id: str) -> CheckpointState:
        raise ProjectionRebuildRequiredError("candidate index needs explicit initial build")

    @abstractmethod
    def require_fresh(self, request: CandidateQuery) -> CandidateCheckpoint: ...


@dataclass(frozen=True)
class NoCheckpoint(CheckpointState):
    byte_offset = 0

    def require_source(self, source: WireSnapshot, root_id: str) -> CheckpointState:
        return self

    def require_fresh(self, request: CandidateQuery) -> CandidateCheckpoint:
        raise ProjectionUnavailableError("candidate projection is stale or unrelated")


@dataclass(frozen=True)
class OutdatedCheckpoint(NoCheckpoint):
    """Disposable rows no longer describe this bus's captured prefix."""

    def refreshed(self, index: WakeCandidateIndex, root_id: str) -> CheckpointState:
        index.maintain(start=self.start())
        # One bounded rebuild, not a retry loop. A later source replacement
        # remains a state; the next maintenance pass owns its next batch.
        return index._captured_checkpoint(root_id)


class CandidateTable:
    """Disposable projections: source identity, recipient hints, and duplicate response keys."""


@dataclass(frozen=True, kw_only=True)
class CandidateCheckpoint(CandidateTable, TypedTable, CheckpointState):
    singleton: Literal[1] = field(metadata={"sql": Column(primary_key=True)})
    version: Literal[3]
    root_id: str
    device: int
    inode: int
    byte_offset: int = field(metadata={"sql": Column(check="byte_offset>=0")})
    tail_digest: str
    last_seq: int = field(metadata={"sql": Column(check="last_seq>=0")})


    @classmethod
    def initial(cls, root_id: str, revision: FileRevision) -> CandidateCheckpoint:
        return cls(singleton=1, version=3, root_id=root_id, device=revision.identity.device,
                   inode=revision.identity.inode, byte_offset=0, tail_digest="", last_seq=0)

    @classmethod
    def current(cls, db: sqlite3.Connection) -> CheckpointState:
        try:
            return next(iter(cls.select(db, singleton=1)), NoCheckpoint())
        except MalformedRecord as error:
            raise ProjectionRebuildRequiredError("candidate index schema is unavailable") from error

    @property
    def source_identity(self) -> PrefixSource:
        return PrefixSource(self.root_id, self.device, self.inode)

    def require_source(self, source: WireSnapshot, root_id: str) -> CheckpointState:
        if self.source_identity != PrefixSource(root_id, source.revision.identity.device, source.revision.identity.inode):
            return OutdatedCheckpoint()
        if self.byte_offset > source.boundary:
            return OutdatedCheckpoint()
        if source.fingerprint(self.byte_offset) != self.tail_digest:
            return OutdatedCheckpoint()
        return self

    def start(self) -> IndexStart:
        return ResumeIndex()

    def checkpoint(self, source: WireSnapshot, root_id: str) -> CandidateCheckpoint:
        return self

    def refreshed(self, index: WakeCandidateIndex, root_id: str) -> CheckpointState:
        return self

    def require_fresh(self, request: CandidateQuery) -> CandidateCheckpoint:
        if self.root_id != request.root_id or self.last_seq < request.required_through_seq:
            raise ProjectionUnavailableError("candidate projection is stale or unrelated")
        return self


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
    """Untrusted scheduling hint, never a sealed claim or native input acknowledgement."""

    root_id: Annotated[str, Id128Text]
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
    response: KeyedResponseAcknowledgement | None


@dataclass(frozen=True, slots=True)
class _ReplayBatch:
    next_offset: int
    last_seq: int
    recipients: tuple[Candidate, ...]
    response_keys: tuple[CandidateResponseKey, ...]


class IndexStart(ABC):
    def for_source(self, checkpoint: CheckpointState, source: WireSnapshot,
                   root_id: str) -> IndexStart:
        """Strict resume and explicit rebuild retain their selected policy."""
        return self

    @abstractmethod
    def start(self, checkpoint: CheckpointState, source: WireSnapshot,
              root_id: str) -> CandidateCheckpoint: ...

    def prepare_rows(self, db: sqlite3.Connection) -> None:
        """Resuming and first builds preserve already committed rows."""

    def prior_bytes(self, index: WakeCandidateIndex, hint: CommittedAppendHint) -> int:
        """A first build or explicit rebuild must enter serialized maintenance."""
        return 0


class ResumeIndex(IndexStart):
    def for_source(self, checkpoint: CheckpointState, source: WireSnapshot,
                   root_id: str) -> IndexStart:
        return checkpoint.require_source(source, root_id).start()

    def start(self, checkpoint: CheckpointState, source: WireSnapshot,
              root_id: str) -> CandidateCheckpoint:
        return checkpoint.require_source(source, root_id).checkpoint(source, root_id)

    def prior_bytes(self, index: WakeCandidateIndex, hint: CommittedAppendHint) -> int:
        return index._verified_checkpoint(hint.root_id).byte_offset


class RebuildIndex(IndexStart):
    def start(self, checkpoint: CheckpointState, source: WireSnapshot,
              root_id: str) -> CandidateCheckpoint:
        return CandidateCheckpoint.initial(root_id, source.revision)

    def prepare_rows(self, db: sqlite3.Connection) -> None:
        db.execute(f'DELETE FROM "{Candidate.declared_name}"')
        db.execute(f'DELETE FROM "{CandidateResponseKey.declared_name}"')


class FirstBuild(ResumeIndex):
    """Start the first maintenance batch without consulting a prior checkpoint.

    The checkpoint state selects resume or rebuild. Schema failure happens
    before selection; malformed storage never permits discarding tables.
    """

    def prior_bytes(self, index: WakeCandidateIndex, hint: CommittedAppendHint) -> int:
        # Validate/adopt under maintain's captured source and revision check,
        # never short-circuit using a checkpoint from a superseded inode.
        return 0


class WakeCandidateIndex(TypedStore):
    """Bounded candidate pages and maintenance of their disposable source state.

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
        if log.path.name != "bus.jsonl":
            raise ValueError("candidate index requires the canonical wire log")
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
                rows.append(
                    Candidate(
                        message.seq,
                        message.message_id,
                        recipient.recipient_lookup,
                        message.sender,
                        message.target,
                        decision.candidate_wake_mode(),
                    )
                )
        return _ParsedRow(sequence, tuple(rows), verified.acknowledgement)

    @classmethod
    def _replay_prefix(cls, source: WireSnapshot, offset: int, last_seq: int, *, max_rows: int, max_bytes: int) -> _ReplayBatch:
        rows, response_keys = [], []
        through = offset
        started = time.monotonic()
        records = iter(source.rows(offset))
        for number in range(max_rows):
            if time.monotonic() - started > cls.batch_seconds or through - offset >= max_bytes:
                break
            try:
                record, _, end = next(records)
            except StopIteration:
                break
            if end - offset > max_bytes and number:
                break
            parsed = cls._parse_row(record, source.metadata.root_id, last_seq)
            last_seq = parsed.seq
            rows.extend(parsed.recipients)
            if parsed.response is not None:
                response_keys.append(CandidateResponseKey(parsed.response.publication_key))
            through = end
        return _ReplayBatch(through, last_seq, tuple(rows), tuple(response_keys))

    def _source_end(self, source: WireSnapshot, next_offset: int) -> str:
        # The checkpoint records this captured source, not a lease on the
        # pathname. Every reader validates it against its own source snapshot.
        return source.fingerprint(next_offset)

    @staticmethod
    def _commit_batch(
        db: sqlite3.Connection,
        batch: _ReplayBatch,
        checkpoint: CandidateCheckpoint,
        tail: str,
        *,
        start: IndexStart,
    ) -> None:
        """Replace derived rows and checkpoint inside the caller's transaction."""
        start.prepare_rows(db)
        for row in batch.recipients:
            row.insert(db)
        for row in batch.response_keys:
            if CandidateResponseKey.one(db, publication_key=row.publication_key) is not None:
                raise ProjectionUnavailableError("duplicate private response publication key")
            row.insert(db)
        replace(checkpoint, byte_offset=batch.next_offset,
                tail_digest=tail, last_seq=batch.last_seq).upsert(db)

    def maintain(
        self, *, start: IndexStart = ResumeIndex(),
        max_rows: int = batch_rows, max_bytes: int = batch_bytes,
    ) -> bool:
        """Replay one bounded prefix; never scan or rebuild on the send/wake path."""
        self._validate_limits(max_rows, max_bytes)
        try:
            with self.log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as source:
                root_id = source.metadata.root_id
                with self.writing() as db:
                    # TypedStore installs schema atomically. Capture the stored
                    # row, then close the SQL connection before reading or decoding.
                    recorded = CandidateCheckpoint.current(db)
                start = start.for_source(recorded, source, root_id)
                checkpoint = start.start(recorded, source, root_id)
                batch = self._replay_prefix(
                    source, checkpoint.byte_offset, checkpoint.last_seq,
                    max_rows=max_rows, max_bytes=max_bytes,
                )
                with self.writing() as db:
                    current = CandidateCheckpoint.current(db)
                    if current == recorded:
                        tail = self._source_end(source, batch.next_offset)
                        self._commit_batch(db, batch, checkpoint, tail, start=start)
                        return batch.next_offset >= source.boundary
                # Another writer's committed row owns progress. Close the SQL
                # connection before validating its source; never replay a batch.
                advanced = ResumeIndex().start(
                    current, source, root_id,
                )
                return advanced.byte_offset >= source.boundary
        except (
            OSError,
            MalformedRecord,
            KeyError,
            OverflowError,
            AttributeError,
            RelationViolationError,
            sqlite3.DatabaseError,
        ) as error:
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


    def _verified_checkpoint(self, root_id: str) -> CheckpointState:
        return self._captured_checkpoint(root_id).refreshed(self, root_id)

    def _captured_checkpoint(self, root_id: str) -> CheckpointState:
        """Capture the row and its source state without rebuilding under a reader."""
        try:
            with self.log.opened_wire_snapshot(opening=SavedPrefixOpening()) as source:
                if source.metadata.root_id != root_id:
                    raise ProjectionRebuildRequiredError("candidate private root changed")
                with self.snapshot() as db:
                    return CandidateCheckpoint.current(db).require_source(source, root_id)
        except (OSError, RelationViolationError, sqlite3.DatabaseError) as error:
            raise ProjectionUnavailableError("candidate checkpoint unavailable") from error

    def catch_up_committed_append(
        self,
        hint: CommittedAppendHint,
        *,
        max_rows: int = batch_rows,
        max_bytes: int = batch_bytes,
        start: IndexStart = ResumeIndex(),
    ) -> CandidateCatchUp:
        """Replay at most ONE bounded batch after a durable append notification.

        Run outside publisher locks and off its latency path. An absent
        derived index may be explicitly bootstrapped in bounded batches; a
        mismatched schema or corrupt WAL never gets an implicit rebuild.
        The checkpoint state adopts a verified changed/truncated source.
        If ``caught_up`` is false and
        ``more_source_bytes`` is true, a deferred worker may schedule another
        finite batch. Pages remain unavailable for required high-water until
        the index catches up. This
        contains no seal, owner, claim or native proof and never retries work.
        """
        hint = hint.validated()
        self._validate_limits(max_rows, max_bytes)
        if self.log.private_marker_unlocked().root_id != hint.root_id:
            raise ProjectionRebuildRequiredError("candidate append hint belongs to another root")
        prior_bytes = start.prior_bytes(self, hint)
        if prior_bytes >= hint.through_bytes:
            return CandidateCatchUp(prior_bytes, True, False)
        # The saved row, not file existence, decides initialization inside
        # maintain's serialized transaction; concurrent first builds can resume.
        more_source_bytes = not self.maintain(
            start=start, max_rows=max_rows, max_bytes=max_bytes
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
    ) -> CandidatePage:
        """Read only a bounded WAL page; caller MUST verify bus+SQL+owner.

        Root and high-water must come from the caller's trusted snapshot, never
        agent/model arguments. Returning a page does not attest current state.
        """
        request = CandidateQuery.capture(root_id=root_id, recipient_lookup=recipient_lookup,
                     after_seq=after_seq, required_through_seq=required_through_seq,
                     limit=limit)
        try:
            self._verified_checkpoint(root_id).require_fresh(request)
            with (self.log.opened_wire_snapshot(opening=SavedPrefixOpening()) as source,
                  self.snapshot() as db):
                checkpoint = (CandidateCheckpoint.current(db)
                              .require_source(source, request.root_id).require_fresh(request))
                data = Candidate.read(
                    db.execute(
                        f'SELECT * FROM "{Candidate.declared_name}" '
                        "WHERE recipient_lookup=? AND source_seq>? "
                        "AND wake_mode IS NOT NULL "
                        "ORDER BY source_seq LIMIT ?",
                        (request.recipient_lookup, request.after_seq, request.limit + 1),
                    )
                )
                return CandidatePage(
                    checkpoint.last_seq,
                    tuple(data[:request.limit]),
                    len(data) > request.limit,
                )
        except (OSError, sqlite3.DatabaseError, MalformedRecord) as error:
            raise ProjectionUnavailableError("candidate projection read unavailable") from error
