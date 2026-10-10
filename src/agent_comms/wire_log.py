"""Canonical append-only wire persistence and durable receipt authority."""

from __future__ import annotations

import json
import math
import os
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import BinaryIO

from agent_comms.coordination_tables.publications import (
    PublicationIntents,
)

from .bus_publication import (
    CommittedDelivery,
    has_private_wire_fields,
    unique_wire_object,
)
from .bus_source_page import AddressedPage
from .bus_projection import BusFileRevision
from .envelope_claim_transitions import (
    ClaimProjection,
    apply_transition,
)
from .errors import (
    RelationViolationError,
)
from .field_codec import FieldCodec
from .messages import Message
from .store_files import (
    StoreLock,
    StoreLockContention,
    _atomic_write_text,
    _iter_jsonl_stream,
    _store_lock,
    _store_lock_file,
    _held_store_source,
)
from .wire_metadata import WireMetadata
from .wire_record import WireRecord, WireScan


@dataclass(frozen=True)
class OpenedWireSnapshot:
    """One original descriptor and fixed cut, owned until the reader exits.

    This is read custody, not permission to append or admit a live turn.
    Uncertified cuts decode every row strictly, including silent observations.
    """

    metadata: WireMetadata
    through: int
    stream: BinaryIO | None
    revision: BusFileRevision | None

    @property
    def boundary(self) -> int:
        return self.revision.size if self.revision is not None else 0

    @staticmethod
    def messages(record: Mapping) -> tuple[Message, ...]:
        return WireRecord.public_from_wire(record).messages()

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
        """Resolve sparse read changes in this cut using the original page index.

        Index coverage and offsets are captured atomically. Missing or stale
        derived evidence falls back to the same bounded source, never absence.
        This reader neither creates nor updates an index.
        """
        import sqlite3
        from .bus_page_index import BusPageIndex, BusPageSource, StaleBusPageIndexError

        if not 0 <= before_offset <= self.boundary:
            raise ValueError("Public read exceeds its opened source cut")
        if not sequences or self.stream is None:
            return
        rows = None
        try:
            with BusPageIndex(Path(self.stream.name), readonly=True) as index:
                index.connection.execute("BEGIN")
                saved = BusPageSource.one(index.connection, singleton=1)
                if saved is not None and saved.covers(self.stream, self.revision):
                    rows = tuple(
                        row for seq in sorted(sequences)
                        for row in index.offsets(lower=seq - 1, upper=seq + 1,
                                                descending=False, targets=None,
                                                before_offset=before_offset)
                    )
        except (OSError, sqlite3.DatabaseError, StaleBusPageIndexError, ValueError, TypeError):
            rows = None
        records = None
        if rows is not None:
            try:
                records = tuple(
                    record for row in rows
                    for record in self.public_page_records(
                        *BusPageIndex.record(self.stream, row,
                                             max_bytes=before_offset - row.offset),
                        self.metadata,
                    )
                )
            except (OSError, StaleBusPageIndexError):
                pass
        if records is not None:
            yield from records
        else:
            yield from super().public_records_for(sequences, before_offset=before_offset)


class WireLog:
    def __init__(self, path: Path):
        # Construction is read-only and needs no registry or publication settings.
        self.path = Path(path)

    @contextmanager
    def locked(self, *, blocking: bool = True, max_bus_bytes: int | None = None,
               contention: StoreLockContention | None = None):
        """The existing canonical bus lock and durability read barrier."""
        with _store_lock(self.path, blocking=blocking, max_bus_bytes=max_bus_bytes,
                         contention=contention) as lock:
            yield lock

    @contextmanager
    def certified_read(self, *, blocking: bool = True, contention: StoreLockContention | None = None):
        """Borrow the original source verified by this canonical lock barrier."""
        with _store_lock(self.path, blocking=blocking, contention=contention) as lock:
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
        """Read an original window through its existing canonical barrier."""
        with _store_lock(self.path) as lock:
            if not self.path.exists():
                return ()
            marker = self._private_marker_unlocked()
            source = lock.certified_read()
            source.require_marker(marker)
            originals = source.conversation_sources(
                lookup, predicate, parameters, limit=limit, ascending=ascending
            )
            source.require_current()
        return tuple(originals)

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

    @contextmanager
    def delivery_snapshot(self):
        """Original routing members derive from the same opened strict read."""
        with self.verified_snapshot() as records:
            yield (item for record in records for item in record.delivery_messages())

    def record_context(self, manifest) -> None:
        """Append the original context projection through its sealed writer.

        Journal-backed values remain source references. Only nonrecoverable
        public contributions are retained in this observation.
        """
        from .wire_record import ContextManifestWireObservation, ObservationWireRecord

        with self.locked():
            marker = self._private_marker_unlocked()
            record = ObservationWireRecord(ContextManifestWireObservation(manifest))
            self._append_private_unlocked(marker, record.to_wire())

    def context_manifests(self, name: str, registry):
        return tuple(resource.value for resource in self.context_manifest_resources(name, registry))

    def context_manifest_resources(self, name: str, registry, *, previous=()):
        """Capture original source and rename membership before decoding.

        The original wire -> bus -> registry order selects the read snapshot;
        neither physical publication lock survives into its decoder.
        """
        with _store_lock(self.path.parent / "wire"):
            with self.certified_read() as source:
                snapshot = registry.snapshot()
                incarnation = snapshot.require(name).incarnation
                captured = source.marker.access.context_resources(
                    source, incarnation, snapshot, previous=previous)
        return tuple(captured)

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

        with _store_lock(self.path.parent / "wire"):
            with self.certified_read() as source:
                snapshot = registry.snapshot()
                owner = snapshot.require(name)
                inputs = InputDispositions(self.path.parent / InputDispositions.filename).read()
                captured = source.retained_task_facts(owner.incarnation)
                export = WireExportBoundary(source.marker.last_seq, time.time())
        yield owner, snapshot, RetainedTaskFacts(tuple(captured)), inputs, export

    def retained_context(self, name: str, registry):
        """Authored retained context; also the source of instruction export.

        This deliberately excludes unpinned inputs, goals and native artifacts.
        CompactionBoundary inspection owns those separate observation scopes.
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

    def _claim_projection_unlocked(self, metadata: WireMetadata) -> tuple[ClaimProjection, int]:
        """Project claims and return the verified bus high-water in one scan."""
        return self._claim_projection(self.verified_records_unlocked(metadata))

    @staticmethod
    def _claim_projection(records) -> tuple[ClaimProjection, int]:
        projection = ClaimProjection()
        verified_sequence = 0
        for record in records:
            verified_sequence = record.sequence_after(verified_sequence)
            for previous in record.messages():
                if previous.claim_transition is not None:
                    projection = apply_transition(projection, previous.claim_transition)
        return projection, verified_sequence

    def claim_projection(self) -> ClaimProjection:
        """Derive ownership exclusively from guarded, verified bus envelopes."""
        with self._opened_wire_snapshot(need_sequence=False) as opened:
            if not opened.metadata.claims:
                raise RelationViolationError("Claim read barrier is unavailable.")
            projection, _verified_sequence = self._claim_projection(
                self._snapshot_records(opened.metadata, opened.stream, opened.boundary)
            )
            return projection

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
                line = stream.readline(scan.max_row_bytes + 1)
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
        metadata.access.require_append()
        encoded = json.dumps(row, allow_nan=False).encode("utf-8") + b"\n"
        if len(encoded) > 8 * 1024 * 1024:
            raise RelationViolationError("Private bus row exceeds the byte limit.")
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

    def read_delivery_cohort(self, wire_root_id: str, wire_seq: int) -> CommittedDelivery:
        """Bus-owned attestation of a committed initial row; no live re-routing."""
        from .audience_manifest import MAX_WIRE_SEQ

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

        if type(intent) is not PublicationIntents:
            raise TypeError("Keyed response requires a validated PublicationIntents.")
        metadata = self._private_marker_unlocked()
        from .private_bus_checkpoint import opened_private_checkpoint_unlocked

        with opened_private_checkpoint_unlocked(self, metadata) as source:
            matched = source.keyed_receipt(intent)
            source.require_current()
            return matched, source.witness.through_seq, metadata

    def read_keyed_response(self, intent: PublicationIntents) -> Message | None:
        """Read-only exact receipt resolution; never append or repair an absent row."""
        with _store_lock(self.path):
            matched, _, _ = self._keyed_receipt_unlocked(intent)
            return matched

    @contextmanager
    def _opened_wire_snapshot(self, *, need_sequence: bool = True):
        """Own one fixed opened inode/byte boundary beyond physical custody."""
        with ExitStack() as resources:
            with _store_lock(self.path) as lock:
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
                revision = BusFileRevision.capture(stream) if stream is not None else None
                snapshot_type = OpenedWireSnapshot
                if lock.source is not None:
                    source = lock.certified_read()
                    source.require_marker(metadata)
                    # A reserved but unappended sequence revokes admission,
                    # not the original committed bytes of this read-only cut.
                    source.require_open_prefix()
                    # Bind the separate retained descriptor to the exact admitted
                    # inode/revision while original physical custody is held.
                    if (stream is None or revision != BusFileRevision.capture(source.stream)
                            or os.fstat(stream.fileno()).st_dev != source.witness.device
                            or boundary != source.witness.offset):
                        raise RelationViolationError("Opened read differs from its certified source cut")
                    snapshot_type = CertifiedOpenedWireSnapshot
                opened = snapshot_type(metadata, through, stream, revision)
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
            raw = stream.readline(min(scan.max_row_bytes + 1, boundary - stream.tell()))
            if not raw:
                break
            yield scan.read(raw)

    @contextmanager
    def page_snapshot(self, *, need_sequence: bool = False):
        """Lend the original opened cut to indexed and sequential page readers."""
        with self._opened_wire_snapshot(need_sequence=need_sequence) as opened:
            yield (opened.metadata, opened.through, opened.revision, opened.stream,
                   opened.public_records())

    @contextmanager
    def _record_snapshot(
        self, *, need_sequence: bool = True
    ) -> Iterator[tuple[int, Iterator[tuple[Message, int]]]]:
        """Public page accounting borrows the one original opened byte boundary."""
        with self.page_snapshot(need_sequence=need_sequence) as (
            _, through, _, _, records,
        ):
            yield through, records

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
        with _store_lock(self.path) as lock:
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
        """Make every visible opt-in bus row durable before ANY bus-lock reader sees it.

        The marker is fsynced before the first claim send. A failed bus append may
        leave a complete, visible row: another cooperating process must fsync the
        opened inode and its directory, then reject incomplete/corrupt rows, before
        treating either the announcement or the claim as committed. This hook is
        entered by the shared bus lock, including ordinary inbox/history readers.
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
        from .private_bus_checkpoint import opened_claim_source_unlocked

        with opened_claim_source_unlocked(self, private_marker, shared=shared) as source:
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
