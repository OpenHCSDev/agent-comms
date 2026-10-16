"""Canonical append-only wire persistence and durable receipt authority."""

from __future__ import annotations

import json
import math
import os
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import BinaryIO

from agent_comms.coordination_tables.publications import (
    PublicationIntents,
)

from .audience_manifest import MAX_WIRE_SEQ
from .bus_publication import (
    CommittedDelivery,
    has_private_wire_fields,
    unique_wire_object,
)
from .bus_source_page import AddressedPage
from .bus_page_index import BusPageIndex, BusPageSource, StaleBusPageIndexError
from .bus_projection import AppendCheckpoint, BusAppendIndex, Checkpoint
from .envelope_claim_transitions import (
    ClaimProjection,
)
from .errors import (
    RelationViolationError,
)
from .field_codec import FieldCodec
from .messages import Message
from .private_path import FileRevision
from .store_files import (
    StoreLock,
    StoreLockContention,
    _atomic_write_text,
    _iter_jsonl_stream,
    _store_lock,
    _store_lock_file,
    _store_read_lock,
    _held_store_source,
)
from .wire_metadata import WireMetadata
from .wire_record import WireRecord, WireScan


def _notify_candidates(log: WireLog, through_bytes: int) -> None:
    from .candidate_maintenance import schedule_candidate_catchup

    schedule_candidate_catchup(log, through_bytes)


@dataclass(frozen=True)
class OpenedWireSnapshot:
    """One original descriptor and fixed cut, owned until the reader exits.

    This is read custody, not permission to append or admit a live turn.
    Uncertified cuts decode every row strictly, including silent observations.
    """

    metadata: WireMetadata
    through: int
    stream: BinaryIO | None
    revision: FileRevision | None
    _append_checkpoints: dict[BusAppendIndex, AppendCheckpoint | None] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )

    def require_page_index_coverage(self, index: BusPageIndex) -> None:
        if self.stream is None or self.revision is None:
            raise RelationViolationError("Index coverage requires an opened wire source")
        saved = BusPageSource.one(index.connection, singleton=1)
        if saved is None:
            raise StaleBusPageIndexError("Page index has no prepared source")
        if not saved.covers(self.stream, self.revision):
            raise StaleBusPageIndexError("Page index does not cover its opened source")

    def query_page_index[T](
        self, read: Callable[[BusPageIndex], T], *, empty: Callable[[], T]
    ) -> T:
        """Prepare/query this cut; source absence and index repair stay owned here."""
        if self.stream is None:
            return empty()
        access = self.metadata.access
        with access.open_page_index(Path(self.stream.name)) as index:
            try:
                with access.prepare_page_index(index, self):
                    return read(index)
            except StaleBusPageIndexError:
                access.repair_page_index(index)
            with access.prepare_page_index(index, self):
                return read(index)

    def indexed_records(self, index: BusPageIndex, rows, *, before_offset: int):
        """Decode selected offsets through this original descriptor and byte cut."""
        if self.stream is None or not 0 <= before_offset <= self.boundary:
            raise RelationViolationError("Indexed read exceeds its opened wire cut")
        for row in rows:
            (message, size), = self.public_page_records(
                *index.record(self.stream, row, max_bytes=before_offset - row.offset),
                self.metadata,
            )
            yield message, size

    def capture_append_indexes(
        self, prepared: tuple[tuple[BusAppendIndex, AppendCheckpoint | None], ...],
    ) -> None:
        self._append_checkpoints.update(prepared)

    def append_checkpoint(self, index: BusAppendIndex) -> AppendCheckpoint | None:
        """Validate the captured candidate only when a consumer reads this cut."""
        record = index.capture(self._append_checkpoints.get(index), self.stream, self.revision)
        self._append_checkpoints[index] = record
        return record

    def remember_append_checkpoint(
        self, index: BusAppendIndex[Checkpoint], record: Checkpoint,
    ) -> None:
        """Keep a computed exact-cut value with this existing source resource."""
        if record.source != self.revision:
            raise ValueError("Computed append checkpoint belongs to another source cut")
        index.hold(record)
        self._append_checkpoints[index] = record

    @property
    def boundary(self) -> int:
        return self.revision.size if self.revision is not None else 0

    def tail_bytes(self, max_bytes: int) -> tuple[bytes, int]:
        """Read trailing bytes from this fixed cut's retained descriptor."""
        if max_bytes < 0:
            raise ValueError("tail byte limit must be nonnegative")
        if self.stream is None:
            return b"", 0
        start = max(0, self.boundary - max_bytes)
        self.stream.seek(start)
        return self.stream.read(self.boundary - start), start

    @staticmethod
    def messages(record: Mapping) -> tuple[Message, ...]:
        from .errors import StoredHistoryUnreadable

        try:
            return WireRecord.public_from_wire(record).messages()
        except (TypeError, ValueError) as error:
            raise StoredHistoryUnreadable("Stored bus history cannot be decoded") from error

    def appended_records(self, previous: FileRevision,
                         previous_sequence: int) -> tuple[WireRecord, ...]:
        """Decode only bytes appended to the same opened source inode."""
        if self.stream is None or self.revision is None:
            raise RelationViolationError("Wire tail has no opened source")
        if previous.identity != self.revision.identity or previous.size > self.revision.size:
            raise RelationViolationError("Wire tail does not extend the settled source")
        self.stream.seek(previous.size)
        scan = WireScan(self.metadata, previous_sequence=previous_sequence)
        records = []
        while self.stream.tell() < self.boundary:
            raw = self.stream.readline(
                min(WireRecord.max_row_bytes + 1, self.boundary - self.stream.tell())
            )
            if not raw:
                break
            records.append(scan.read(raw))
        if scan.previous_sequence != self.metadata.last_seq:
            raise RelationViolationError("Wire tail does not reach its marker")
        if FileRevision.from_file(self.stream) != self.revision:
            raise RelationViolationError("Wire tail changed while it was read")
        return tuple(records)

    def public_records(self, offset: int = 0, *, before_offset: int | None = None
                       ) -> Iterator[tuple[Message, int]]:
        if self.stream is None:
            return
        boundary = self.boundary if before_offset is None else before_offset
        if not 0 <= offset <= boundary <= self.boundary:
            raise ValueError("Public read exceeds its opened source cut")
        self.stream.seek(offset)
        for record, raw_size in _iter_jsonl_stream(
            self.stream, boundary=boundary, label="wire snapshot"
        ):
            yield from self.public_page_records(record, raw_size, self.metadata)

    def public_records_for(self, sequences: frozenset[int], *, before_offset: int
                           ) -> Iterator[tuple[Message, int]]:
        """An uncertified cut must validate every original row, including silence."""
        if not 0 <= before_offset <= self.boundary:
            raise ValueError("Public read exceeds its opened source cut")
        if sequences:
            for message, size in self.public_records(before_offset=before_offset):
                if message.seq in sequences:
                    yield message, size

    @classmethod
    def public_page_records(cls, record: Mapping, raw_size: int,
                            metadata: WireMetadata) -> Iterator[tuple[Message, int]]:
        """Silent facts consume physical bytes, never message/display budgets."""
        for message in cls.messages(record):
            if has_private_wire_fields(record):
                yield message, len(json.dumps(message.to_wire()).encode()) + 1
            else:
                message.require_retained_admission(metadata.admission_after_seq)
                yield message, raw_size


class CertifiedOpenedWireSnapshot(OpenedWireSnapshot):
    """Public projection of a cut admitted by the acquired complete WireScan.

    Only original locked acquisition can select this implementation. Later
    appends stay outside the cut; the closed SQLite reader grants no authority.
    """

    @staticmethod
    def messages(record: Mapping) -> tuple[Message, ...]:
        return WireRecord.certified_public_messages(record)

    def public_records_for(self, sequences: frozenset[int], *, before_offset: int
                           ) -> Iterator[tuple[Message, int]]:
        """Resolve sparse changes through the same prepared index as page reads."""
        if not 0 <= before_offset <= self.boundary:
            raise ValueError("Public read exceeds its opened source cut")
        if not sequences:
            return

        def read(index):
            rows = (
                row for seq in sorted(sequences)
                for row in index.offsets(lower=seq - 1, upper=seq + 1,
                                        descending=False, targets=None,
                                        before_offset=before_offset)
            )
            return tuple(self.indexed_records(index, rows, before_offset=before_offset))

        yield from self.query_page_index(read, empty=tuple)


@dataclass(frozen=True, slots=True)
class ConversationSourceWindowRequest:
    """One bounded indexed conversation selection from a certified wire cut."""

    predicate: str
    parameters: tuple
    limit: int
    ascending: bool

    def capture(self, source, lookup: str) -> Iterator[CommittedDelivery]:
        """Copy selected bytes now; decode them only as the consumer advances."""
        return (
            source.conversation_sources(
                lookup,
                self.predicate,
                self.parameters,
                limit=self.limit,
                ascending=self.ascending,
            )
            if self.limit
            else iter(())
        )

    @classmethod
    def frontier(cls, log: WireLog, lookup: str) -> int:
        """Read one conversation high-water without reconstructing a delivery."""
        if not log.path.exists() and not log.metadata_path.exists():
            return 0
        with log.certified_read() as source:
            return source.conversation_frontier(lookup)

    @classmethod
    def capture_many(
        cls,
        log: WireLog,
        lookup: str,
        requests: tuple[ConversationSourceWindowRequest, ...],
    ) -> tuple[Iterator[CommittedDelivery], ...]:
        """Copy each selection from one source; consumers decode after release."""
        if not log.path.exists() and not log.metadata_path.exists():
            return tuple(iter(()) for request in requests)
        with log.certified_read() as source:
            return tuple(request.capture(source, lookup) for request in requests)


class WireLog:
    def __init__(self, path: Path):
        # Construction is read-only and needs no registry or publication settings.
        self.path = Path(path)
        self._append_indexes: list[BusAppendIndex] = []

    def register_append_index(self, index: BusAppendIndex[Checkpoint]) -> BusAppendIndex[Checkpoint]:
        """Register one index owner for automatic capture with every opened cut."""
        if index.source_path != self.path:
            raise ValueError("Append index belongs to another bus")
        if index not in self._append_indexes:
            self._append_indexes.append(index)
        return index

    @contextmanager
    def locked(self, *, blocking: bool = True, max_bus_bytes: int | None = None,
               contention: StoreLockContention | None = None):
        """The existing canonical bus lock and durability read barrier."""
        with _store_lock(self.path, blocking=blocking, max_bus_bytes=max_bus_bytes,
                         contention=contention) as lock:
            yield lock

    @contextmanager
    def certified_read(self, *, blocking: bool = True, contention: StoreLockContention | None = None):
        """Borrow the original source verified by the canonical reader barrier."""
        with _store_read_lock(self.path, blocking=blocking, contention=contention) as lock:
            with self._certified_source(lock) as source:
                yield source

    async def read_certified_async(self, read: Callable):
        """Acquire cancellably, then consume the whole source in its worker.

        Return detached observations only. Verification and the callback share
        one resource thread; no SQLite connection crosses to the event loop.
        The worker closes physical custody before returning its result; result
        delivery must not keep a POSIX lock until the event loop resumes.
        Cancellation joins acquired work before the original descriptor closes.
        """
        from .private_bus_checkpoint import CheckpointNeedsRepair

        # Readers share the bus lock so idle workers don't queue behind each
        # other; only a checkpoint repair needs the exclusive lock.
        try:
            return await self._read_checked_under_lock(read, shared=True)
        except CheckpointNeedsRepair:
            return await self._read_checked_under_lock(read, shared=False)

    async def _read_checked_under_lock(self, read: Callable, *, shared: bool):
        from .child_process import Platform
        from .coordinator import Coordination

        platform = Platform.current()
        with _store_lock_file(self.path) as lock_file:
            await StoreLockContention(math.inf).acquire_async(
                lock_file.fileno(), platform, shared=shared,
            )
            return await Coordination.run_worker(
                partial(self._read_certified, lock_file, platform, read, shared),
            )

    def _read_certified(self, lock_file, platform, read, shared):
        with lock_file, _held_store_source(self.path, lock_file, platform, None,
                                           shared=shared) as lock:
            with self._certified_source(lock) as source:
                return read(source)

    @contextmanager
    def _certified_source(self, lock: StoreLock):
        """Sync and async acquisition consume the same marker/currentness owner."""
        marker = self._private_marker_unlocked()
        source = lock.certified_read()
        source.require_marker(marker)
        yield source
        source.require_current()

    def conversation_sources(self, lookup, predicate, parameters, *, limit, ascending):
        """Copy a bounded window under the lock; decode after releasing it."""
        request = ConversationSourceWindowRequest(predicate, parameters, limit, ascending)
        return request.capture_many(self, lookup, (request,))[0]

    def addressed_sources(self, lookup: str, after_seq: int = 0) -> Iterator[CommittedDelivery]:
        """Borrow bounded pages from one original committed append-only cut.

        A later append cannot extend this iteration. Each page captures exact
        original pointers and bytes inside certification; validation and all
        consumer work occur after its publication custody has closed.
        """
        request = AddressedPage(lookup=lookup, after_seq=after_seq)
        with self.certified_read() as source:
            witness, captured, more = source.addressed_page(self, request)
        while True:
            originals = tuple(captured)
            yield from originals
            if not more:
                return
            request = replace(request, after_seq=originals[-1].message.seq)
            with self.certified_read() as source:
                _, captured, more = source.addressed_page(self, request, prefix=witness)

    def full_history(self) -> list[Message]:
        with self.verified_snapshot() as records:
            return [message for record in records for message in record.messages()]

    def record_context(self, manifest) -> None:
        """Append the context records chosen by the certified entry owner."""
        with self.locked() as lock:
            marker = self._private_marker_unlocked()
            for record in lock.certified_read().context_records(manifest):
                self._append_private_unlocked(marker, record.to_wire())

    def context_manifest_page(self, name: str, registry, *, cursor=None, previous=()):
        from .private_bus_checkpoint import RecordedContextManifestSources

        return RecordedContextManifestSources.for_owner(
            self, name, registry, cursor=cursor, previous=previous)

    @contextmanager
    def retained_sources(self, name: str, registry):
        """Borrow one certified wire/registry/input cut for retained readers.

        Capture original bytes under the existing wire/bus/registry/input cut,
        then decode outside publication custody. This is an observation, never
        a current admission or compaction permit.
        """
        from .exporting import WireExportBoundary
        from .input_disposition import InputDispositions
        from .retained_task_facts import RetainedTaskFacts

        with _store_lock(self.path.parent / "wire", shared=True), self.certified_read() as source:
            snapshot = registry.snapshot()
            owner = snapshot.require(name)
            inputs = InputDispositions(self.path.parent)
            captured = source.retained_task_facts(owner.incarnation)
            export = WireExportBoundary(source.marker.last_seq, time.time())
        yield owner, snapshot, RetainedTaskFacts(tuple(captured)), inputs, export

    def retained_context(self, name: str, registry):
        """Authored retained context; also the source of instruction export.

        This deliberately excludes unpinned inputs, goals and native artifacts.
        Their own owners present those separately.
        """
        from .context_segments.retained import RetainedSegment
        from .retained_task_facts import RetainedTaskFacts

        with self.retained_sources(name, registry) as (owner, snapshot, facts, inputs, export):
            retained = RetainedTaskFacts((*facts.facts, *facts.original_input_facts(inputs)))
            return RetainedSegment.capture(retained.for_owner(owner, snapshot),
                                           owner, snapshot, export)

    def _assert_private_directory(self) -> None:
        """Require a nonredirectable, owned ancestry (root sticky /tmp permitted)."""
        if os.name != "posix":
            raise RelationViolationError("Private bus requires POSIX ownership and modes.")
        # Walk the lexical absolute spelling, not only a relative root up to
        # Path('.'); resolve() would hide symlink ancestors instead of rejecting them.
        if ".." in self.path.parts:
            raise RelationViolationError("Private bus directory ancestry is not trusted.")
        path = self.path.parent.absolute()
        while True:
            info = path.lstat()
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in {0, os.geteuid()}
                or (info.st_mode & 0o022 and not sticky_root)
            ):
                raise RelationViolationError("Private bus directory ancestry is not trusted.")
            if path == path.parent:
                break
            path = path.parent

    def claim_projection(self) -> ClaimProjection:
        """Read current ownership from the canonical certified checkpoint."""
        with self.certified_read() as source:
            return source.claim_projection()

    def _private_marker_unlocked(self) -> WireMetadata:
        self._assert_private_directory()
        if self.path.parent.lstat().st_uid != os.geteuid():
            raise RelationViolationError("Private bus directory is not owner-controlled.")
        sequence_path = self.metadata_path
        repair_path = self.path.with_name(self.path.name + ".corrupt")
        for path in (sequence_path, self.path, repair_path):
            if not path.exists() and not path.is_symlink():
                continue
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise RelationViolationError("Private bus files require regular owner-only mode.")
        if not sequence_path.exists():
            raise RelationViolationError("Private bus writer has no durable protocol marker.")
        metadata = self.read_metadata_unlocked(required=True)
        if not metadata.private:
            raise RelationViolationError("Private bus writer has no durable protocol marker.")
        return metadata

    def verified_records_unlocked(
        self,
        metadata: WireMetadata,
        *,
        on_row: Callable[[int, bytes, WireRecord], None] | None = None,
    ) -> Iterator[WireRecord]:
        """Entire strict stream; later corruption cannot certify an earlier append."""
        scan = WireScan(metadata)
        if not self.path.exists():
            return
        with self.path.open("rb") as stream:
            while True:
                offset = stream.tell()
                line = stream.readline(WireRecord.max_row_bytes + 1)
                if not line:
                    break
                record = scan.read(line)
                if on_row is not None:
                    on_row(offset, line, record)
                yield record

    def _append_private_unlocked(self, metadata: WireMetadata, row: Mapping[str, object]) -> None:
        """Durably reserve a sequence, append one row, then sync its parent.

        A failed append or bus parent sync leaves the outcome UNKNOWN.
        """
        StoreLock.current_release().require_active()
        metadata.access.require_append()
        encoded = json.dumps(row, allow_nan=False).encode("utf-8") + b"\n"
        WireRecord.require_row_size(encoded, message="Private bus row exceeds the byte limit.")
        record = WireRecord.from_wire(row, metadata.root_id)
        metadata.last_seq = record.sequence_after(metadata.last_seq)
        self.write_metadata_unlocked(metadata)
        descriptor = os.open(
            self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise RelationViolationError("Private bus append target is not owner-only.")
            with os.fdopen(descriptor, "ab", closefd=False) as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
        finally:
            os.close(descriptor)
        directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        from .private_bus_checkpoint import (
            append_private_bus_checkpoint_unlocked,
            certificate_enabled,
        )

        if certificate_enabled(self.path):
            try:
                append_private_bus_checkpoint_unlocked(self, metadata, encoded, record)
            except Exception as error:
                # The bus may already contain this fsynced row. Never retry an
                # uncertain input or report publication as definitely absent.
                raise RelationViolationError(
                    "Private bus checkpoint publication outcome UNKNOWN."
                ) from error
        StoreLock.current_release().callback(_notify_candidates, self, self.path.stat().st_size)

    def read_delivery_cohort(self, wire_root_id: str, wire_seq: int) -> CommittedDelivery:
        """Bus-owned attestation of a committed initial row; no live re-routing."""
        if type(wire_seq) is not int or not 0 < wire_seq <= MAX_WIRE_SEQ:
            raise ValueError("wire_seq must be a positive SQLite-range integer.")
        with self.certified_read() as source:
            if wire_root_id != source.witness.root_id:
                raise RelationViolationError("Initial wire root does not match the bus marker.")
            return source.delivery(wire_seq)

    def delivery_cohorts_unlocked(self, wire_root_id: str, sequences):
        """Borrow canonical checkpoint resources under the caller's bus custody."""
        from .private_bus_checkpoint import opened_private_checkpoint_unlocked

        marker = self._private_marker_unlocked()
        if marker.root_id != wire_root_id:
            raise RelationViolationError("Initial wire root does not match the bus marker.")
        with opened_private_checkpoint_unlocked(self, marker) as source:
            originals = tuple(source.delivery(seq) for seq in sequences)
            source.require_current()
            return originals

    def _keyed_receipt_unlocked(
        self, intent: PublicationIntents
    ) -> tuple[Message | None, int, WireMetadata]:
        """Resolve the original receipt through the canonical sealed prefix.

        Caller holds the bus file lock. Absence is NOT authorization to append.
        """

        metadata = self._private_marker_unlocked()
        from .private_bus_checkpoint import opened_private_checkpoint_unlocked

        with opened_private_checkpoint_unlocked(self, metadata) as source:
            matched = source.keyed_receipt(intent)
            source.require_current()
            return matched, source.witness.through_seq, metadata

    def read_keyed_response(self, intent: PublicationIntents) -> Message | None:
        """Read-only exact receipt resolution; never append or repair an absent row."""
        with self.certified_read() as source:
            return source.keyed_receipt(intent)

    def prepare_append_indexes(self) -> tuple[tuple[BusAppendIndex, AppendCheckpoint | None], ...]:
        """Read registered index candidates before taking any source locks."""
        return tuple((index, index.prepare()) for index in self._append_indexes)

    @contextmanager
    def _opened_wire_snapshot(
        self, *, need_sequence: bool = True,
        prepared_indexes: tuple[tuple[BusAppendIndex, AppendCheckpoint | None], ...] = (),
    ):
        """Own one fixed opened inode/byte boundary beyond physical custody."""
        with ExitStack() as resources:
            with _store_read_lock(self.path) as lock:
                metadata = (
                    self._private_marker_unlocked()
                    if self.path.exists() or self.metadata_path.exists()
                    else WireMetadata()
                )
                through = self._committed_sequence_unlocked(lock) if need_sequence else 0
                try:
                    stream = resources.enter_context(self.path.open("rb"))
                except FileNotFoundError:
                    stream = None
                boundary = stream.seek(0, 2) if stream is not None else 0
                if stream is not None:
                    stream.seek(0)
                revision = FileRevision.from_file(stream) if stream is not None else None
                snapshot_type = OpenedWireSnapshot
                if lock.source is not None:
                    source = lock.certified_read()
                    source.require_marker(metadata)
                    # A reserved but unappended sequence revokes admission,
                    # not the original committed bytes of this read-only cut.
                    source.require_open_prefix()
                    # Bind the separate retained descriptor to the exact admitted
                    # inode/revision while original physical custody is held.
                    if (stream is None or revision != FileRevision.from_file(source.stream)
                            or os.fstat(stream.fileno()).st_dev != source.witness.device
                            or boundary != source.witness.offset):
                        raise RelationViolationError("Opened read differs from its certified source cut")
                    snapshot_type = CertifiedOpenedWireSnapshot
                opened = snapshot_type(metadata, through, stream, revision)
            # Transfer candidate records without reading files or hashing here.
            # Consumers validate after the caller's outer locks are released.
            opened.capture_append_indexes(prepared_indexes)
            yield opened

    @contextmanager
    def verified_snapshot(self) -> Iterator[Iterator[WireRecord]]:
        """Run the original strict WireScan after releasing publication custody.

        Uncertified streams retain the same complete validation algorithm.
        This resource is a fixed read, not a current append/admission permit.
        """
        with self._opened_wire_snapshot(need_sequence=False) as opened:
            yield self._snapshot_records(opened.metadata, opened.stream, opened.boundary)

    @staticmethod
    def _snapshot_records(metadata, stream, boundary):
        scan = WireScan(metadata)
        while stream is not None and stream.tell() < boundary:
            raw = stream.readline(min(WireRecord.max_row_bytes + 1, boundary - stream.tell()))
            if not raw:
                break
            yield scan.read(raw)

    @contextmanager
    def _record_snapshot(
        self, *, need_sequence: bool = True
    ) -> Iterator[tuple[int, Iterator[tuple[Message, int]]]]:
        """Explicit sequential history borrows the original opened byte boundary."""
        with self._opened_wire_snapshot(need_sequence=need_sequence) as opened:
            yield opened.through, opened.public_records()

    @contextmanager
    def full_history_snapshot(self) -> Iterator[tuple[int, Iterator[Message]]]:
        """Open one fixed append boundary without retaining the complete wire."""
        with self._record_snapshot() as (through, records):
            yield through, (message for message, _ in records)

    def message_by_id(self, message_id: str) -> Message | None:
        """Resolve an ID-only request through the canonical opened wire stream.

        Display callers carry original seq/id references and use their bounded
        certified window. There is no second receipt-offset index or cache.
        """
        with self.full_history_snapshot() as (_, messages):
            return next((message for message in messages if message.message_id == message_id), None)

    def messages_for_references(self, references):
        return tuple(source.message for source in self.deliveries_for_references(references))

    def deliveries_for_references(self, references):
        """One canonical lock/certificate lifetime for a visible source window."""
        if not references:
            return ()
        with self.certified_read() as source:
            originals = source.references(references)
        return tuple(originals)

    def total_messages(self) -> int:
        with self.verified_snapshot() as records:
            return sum(1 for record in records for _ in record.messages())

    def latest_sequence(self) -> int:
        """Return the global high-water sequence without loading message bodies."""
        with _store_read_lock(self.path) as lock:
            return self._committed_sequence_unlocked(lock)

    def _iter_log_unlocked(self) -> Iterator[Message]:
        if self.path.exists():
            marker = self._private_marker_unlocked()
            for record in self.verified_records_unlocked(marker):
                yield from record.messages()

    def _committed_sequence_unlocked(self, lock: StoreLock) -> int:
        """Read the committed cut from the resource acquired by this barrier.

        The certificate's global through_seq is not the addressed page's
        latest_source_seq or a marker reservation. Claim streams without an
        installed certificate still require their original strict traversal.
        """
        if lock.source is not None:
            return lock.certified_read().committed_sequence()
        if self.claim_gate_enabled():
            return max((message.seq for message in self._iter_log_unlocked()), default=0)
        return self.read_metadata_unlocked().last_seq

    def claim_gate_enabled(self) -> bool:
        return self._claim_marker_unlocked() is not None

    def _claim_marker_unlocked(self) -> WireMetadata | None:
        # _store_lock is also used for registry, channels, and marker files.
        # Only the canonical bus may enter this read/durability barrier.
        if self.path.name != "bus.jsonl":
            return None
        metadata = self.read_metadata_unlocked()
        from .private_bus_checkpoint import certificate_enabled

        if certificate_enabled(self.path) and not metadata.claims:
            raise RelationViolationError("Private checkpoint lacks its claim read barrier.")
        return metadata if metadata.claims else None

    @contextmanager
    def verify_before_read_unlocked(self, *, shared: bool = False):
        """Read a durably certified source, recovering unfinished writes exclusively.

        A final certificate proves its exact bus revision was flushed by the
        writer. Shared readers verify that certificate without flushing again.
        Recovery and uncertified sources still flush before accepting visible
        bytes left by an interrupted append.
        """
        private_marker = self._claim_marker_unlocked()
        if private_marker is None:
            yield None
            return
        marker = self.metadata_path
        marker_info = marker.lstat()
        if (
            os.name != "posix"
            or not stat.S_ISREG(marker_info.st_mode)
            or marker_info.st_uid != os.geteuid()
            or stat.S_IMODE(marker_info.st_mode) != 0o600
        ):
            raise RelationViolationError("Claim bus read barrier is not durable and private.")
        from .private_bus_checkpoint import opened_private_checkpoint_unlocked

        with opened_private_checkpoint_unlocked(self, private_marker, shared=shared) as source:
            yield source

    @property
    def metadata_path(self) -> Path:
        return self.path.with_name("bus_meta.json")

    def read_metadata_unlocked(self, *, required: bool = False) -> WireMetadata:
        """Read one marker inode under its leaf lock, independently of registry reads.

        Callers still own any required bus/registry transaction. Marker ownership
        is checked on the opened inode before this lock lets publication retire
        it; zero or multiple links are never accepted as owner-only storage.
        """
        with _store_lock(self.metadata_path, shared=True):
            try:
                descriptor = os.open(
                    self.metadata_path,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                )
            except FileNotFoundError as error:
                if required or self.metadata_path.is_symlink() or self.path.exists():
                    raise RelationViolationError(
                        "Bus protocol marker is missing or redirected."
                    ) from error
                return WireMetadata()
            except OSError as error:
                raise RelationViolationError(
                    "Bus protocol marker is unreadable or redirected."
                ) from error
            with os.fdopen(descriptor, encoding="utf-8") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise RelationViolationError("Bus protocol marker must be regular storage.")
                try:
                    data = json.load(source, object_pairs_hook=unique_wire_object)
                except (ValueError, UnicodeError) as error:
                    raise RelationViolationError("Bus protocol marker is malformed.") from error
            try:
                metadata = FieldCodec.decode(WireMetadata, data)
                # Omitted optional fields express absence. Explicit nulls, missing
                # required public sequence and mixed protocol rows are not aliases.
                if FieldCodec.encode(metadata) != data:
                    raise ValueError("Noncanonical bus protocol marker")
            except (TypeError, ValueError) as error:
                raise RelationViolationError(f"Bus protocol marker is invalid: {error}") from error
            if metadata.private and (
                info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise RelationViolationError("Private bus protocol marker is not owner-only")
            return metadata

    def write_metadata_unlocked(self, metadata: WireMetadata) -> None:
        # Leaf lock order: bus/registry (when needed) -> marker. Marker operations
        # never acquire a bus or registry lock. Keep replacement AND its parent
        # fsync inside this boundary so readers cannot observe retired inodes.
        with _store_lock(self.metadata_path):
            _atomic_write_text(
                self.metadata_path,
                json.dumps(FieldCodec.encode(metadata), indent=2),
                fsync_parent=True,
            )

    def require_fresh_private_root_unlocked(self) -> None:
        self._assert_private_directory()
        root_info = self.path.parent.lstat()
        if root_info.st_uid != os.geteuid() or stat.S_IMODE(root_info.st_mode) != 0o700:
            raise RelationViolationError("Private bus directory must be owner-only.")
        repair = self.path.with_name(self.path.name + ".corrupt")
        if any(
            path.exists() or path.is_symlink() for path in (self.metadata_path, self.path, repair)
        ):
            raise RelationViolationError(
                "Existing unmarked bus data is read-only until its history is rewritten "
                "into the current source format."
            )
