"""Canonical append-only wire persistence and durable acknowledgement authority."""

from __future__ import annotations

import json
import hashlib
import math
import os
import sqlite3
import stat
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from io import DEFAULT_BUFFER_SIZE
from pathlib import Path
from typing import BinaryIO, TYPE_CHECKING

from agent_comms.coordination_tables.publications import (
    PublicationIntents,
)

from .audience_manifest import MAX_WIRE_SEQ
from .bus_publication import (
    CommittedDelivery,
    has_private_wire_fields,
)
from .bus_source_page import AddressedPage
from .bus_page_index import BusPageIndex, BusPageRow
from .bus_projection import (AppendCheckpoint, BusAppendIndex, Checkpoint, NoCheckpoint,
                             WirePrefix, EmptyWirePrefix, CompleteWirePrefix)
from .envelope_claim_transitions import (
    ClaimProjection,
)
from .errors import (
    RelationViolationError,
    StoredHistoryUnreadable,
)
from .coordination_errors import StaleRevision
from .field_codec import FieldCodec, MalformedRecord
from .messages import Message
from .recorded_bus import NoRecordedBus, RecordedBus, RecordedBusIdentity
from .private_path import AbsentPath, FileIdentity, FileRevision, ObservedPath
from .private_bus_checkpoint import RecordedContextManifestSources
from .store_files import (
    ExclusiveLock,
    LockMode,
    SharedLock,
    StoreLock,
    StoreLockOwner,
    StoreLockContention,
    _atomic_write_text,
    _iter_jsonl_stream,
    _store_lock,
    _store_lock_file,
    _held_store_source,
)
from .wire_metadata import WireMetadata
from .wire_record import WireRecord, WireScan

if TYPE_CHECKING:
    from .message_page import MessageReadRequest


def _notify_candidates(log: WireLog, through_bytes: int) -> None:
    from .candidate_maintenance import schedule_candidate_catchup

    schedule_candidate_catchup(log, through_bytes)


@dataclass(frozen=True)
class WireSnapshot(ObservedPath):
    """One fixed bus cut; its concrete source owns availability and reading."""

    metadata: WireMetadata
    through: int
    _append_checkpoints: dict[BusAppendIndex, AppendCheckpoint | NoCheckpoint] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )

    @property
    @abstractmethod
    def complete_prefix(self) -> WirePrefix: ...

    @property
    @abstractmethod
    def boundary(self) -> int: ...

    @property
    @abstractmethod
    def position(self) -> int: ...

    @abstractmethod
    def changed_since(self, revision) -> bool: ...



    if TYPE_CHECKING:
        revision: FileRevision

    @property
    def bus_identity(self) -> RecordedBusIdentity:
        return RecordedBus(self.revision.identity)

    @abstractmethod
    def rows(self, after_offset: int = 0, before_offset: int | None = None): ...

    @abstractmethod
    def fingerprint(self, offset: int) -> str: ...

    @abstractmethod
    def checked(self, source) -> OpenedWireSnapshot: ...

    @abstractmethod
    def records(self) -> Iterator[WireRecord]: ...

    @abstractmethod
    def query_page_index[T](self, read: Callable[[BusPageIndex], T]) -> T: ...

    @abstractmethod
    def indexed_records(self, rows: Iterable[BusPageRow], *, before_offset: int) -> Iterator[tuple[Message, int]]: ...

    @abstractmethod
    def append_checkpoint(self, index: BusAppendIndex) -> AppendCheckpoint | NoCheckpoint: ...

    @abstractmethod
    def remember_append_checkpoint(self, index: BusAppendIndex[Checkpoint], record: Checkpoint) -> None: ...

    @abstractmethod
    def _tail_bytes(self, max_bytes: int) -> tuple[bytes, int]: ...

    @abstractmethod
    def appended_records(self, previous: FileRevision, previous_sequence: int) -> tuple[WireRecord, ...]: ...

    @abstractmethod
    def public_records(self, offset: int = 0, *, before_offset: int | None = None) -> Iterator[tuple[Message, int]]: ...

    def capture_append_indexes(self, prepared) -> None:
        self._append_checkpoints.update(prepared)

    def tail_bytes(self, max_bytes: int) -> tuple[bytes, int]:
        if max_bytes < 0:
            raise ValueError("tail byte limit must be nonnegative")
        return self._tail_bytes(max_bytes)

    @staticmethod
    def messages(record: Mapping) -> tuple[Message, ...]:
        from .errors import StoredHistoryUnreadable

        try:
            return WireRecord.public_from_wire(record).messages()
        except MalformedRecord as error:
            raise StoredHistoryUnreadable("Stored bus history cannot be decoded") from error

    def messages_for_references(self, references):
        """Resolve exact public identities, including rows without a wake cohort."""
        if not references:
            return ()
        sequences = frozenset(reference.seq for reference in references)
        boundary = self.complete_prefix.boundary
        messages = {
            message.reference: message
            for message, _ in self.query_page_index(lambda index: tuple(self.indexed_records(
                index.sequence_offsets(sequences, before_offset=boundary), before_offset=boundary)))
        }
        if any(reference not in messages for reference in references):
            raise RelationViolationError("Notification reference is not its original source")
        return tuple(messages[reference] for reference in references)

    @classmethod
    def public_page_records(cls, record: Mapping, raw_size: int,
                            metadata: WireMetadata) -> Iterator[tuple[Message, int]]:
        """Silent facts consume physical bytes, never message/display budgets."""
        for message in cls.messages(record):
            if has_private_wire_fields(record):
                yield message, len(json.dumps(message.to_wire()).encode()) + 1
            else:
                message.require_retained_claim(metadata.claimed_after_seq)
                yield message, raw_size


class AbsentWireSnapshot(WireSnapshot, AbsentPath):
    """No descriptor existed at acquisition; later appends stay outside this cut."""

    complete_prefix = EmptyWirePrefix()
    boundary = position = 0

    @property
    def bus_identity(self) -> RecordedBusIdentity:
        return NoRecordedBus()

    def changed_since(self, revision) -> bool:
        return revision is not None



    @property
    def revision(self) -> FileRevision:
        raise FileNotFoundError("An absent wire cut has no file revision")

    def rows(self, after_offset: int = 0, before_offset: int | None = None):
        return iter(())

    def fingerprint(self, offset: int) -> str:
        return hashlib.sha256(b"").hexdigest()

    def checked(self, source) -> OpenedWireSnapshot:
        raise RelationViolationError("Opened read differs from its certified source cut")

    def records(self) -> Iterator[WireRecord]:
        return iter(())

    def query_page_index[T](self, read: Callable[[BusPageIndex], T]) -> T:
        from .bus_page_index import EmptyBusPageIndex
        return read(EmptyBusPageIndex())

    def indexed_records(self, rows: Iterable[BusPageRow], *, before_offset: int) -> Iterator[tuple[Message, int]]:
        if before_offset or any(True for _ in rows):
            raise RelationViolationError("Indexed read exceeds its opened wire cut")
        return iter(())

    def append_checkpoint(self, index: BusAppendIndex) -> NoCheckpoint:
        return NoCheckpoint()

    def remember_append_checkpoint(self, index: BusAppendIndex[Checkpoint], record: Checkpoint) -> None:
        raise ValueError("Computed append checkpoint belongs to another source cut")

    def _tail_bytes(self, max_bytes: int) -> tuple[bytes, int]:
        return b"", 0

    def appended_records(self, previous: FileRevision, previous_sequence: int) -> tuple[WireRecord, ...]:
        raise RelationViolationError("Wire tail has no opened source")

    def public_records(self, offset: int = 0, *, before_offset: int | None = None) -> Iterator[tuple[Message, int]]:
        return iter(())


@dataclass(frozen=True)
class OpenedWireSnapshot(WireSnapshot):
    """One original descriptor and required revision, held until the reader exits."""

    stream: BinaryIO
    revision: FileRevision
    page_index_type: type[BusPageIndex]

    @property
    def position(self) -> int:
        return self.stream.tell()

    def same_file(self, identity: FileIdentity) -> bool:
        return self.revision.identity == identity

    def changed_since(self, revision) -> bool:
        return self.revision != revision



    def rows(self, after_offset: int = 0, before_offset: int | None = None):
        """Complete rows whose starts fall within the requested original cut."""
        stop = self.boundary if before_offset is None else min(before_offset, self.boundary)
        if after_offset < 0 or stop < 0:
            raise MalformedRecord("Wire row offsets must be nonnegative")
        self.stream.seek(after_offset)
        for offset, raw in WireRecord.rows(self.stream, stop, self.boundary):
            if raw.strip():
                record = FieldCodec.parse(raw)
                if not isinstance(record, Mapping):
                    raise MalformedRecord("JSONL bus row must be an object")
                yield record, offset, self.stream.tell()

    def fingerprint(self, offset: int) -> str:
        return AppendCheckpoint.fingerprint(self.stream, offset)

    def checked(self, source) -> OpenedWireSnapshot:
        if (self.revision != FileRevision.from_file(source.stream)
                or os.fstat(self.stream.fileno()).st_dev != source.witness.device
                or self.boundary != source.witness.offset):
            raise RelationViolationError("Opened read differs from its certified source cut")
        return replace(self)

    def records(self) -> Iterator[WireRecord]:
        scan = WireScan(self.metadata)
        for _, raw in WireRecord.rows(self.stream, self.boundary, self.boundary):
            yield scan.read(raw)

    @property
    def complete_prefix(self) -> WirePrefix:
        """Decode prefix presence at the last complete line of this fixed cut."""
        stream, revision = self.stream, self.revision
        if not revision.opened_by(stream):
            raise StaleRevision("Opened wire stream no longer contains its captured revision")
        position = stream.tell()
        try:
            end = revision.size
            if end:
                stream.seek(end - 1)
                if stream.read(1) == b"\n":
                    return CompleteWirePrefix(end)
            while end:
                start = max(0, end - DEFAULT_BUFFER_SIZE)
                stream.seek(start)
                newline = stream.read(end - start).rfind(b"\n")
                if newline >= 0:
                    return CompleteWirePrefix(start + newline + 1)
                end = start
            return EmptyWirePrefix()
        finally:
            stream.seek(position)

    def query_page_index[T](
        self, read: Callable[[BusPageIndex], T]
    ) -> T:
        """Query this cut without writing or repairing its derived offsets."""
        if self.boundary == 0:
            from .bus_page_index import EmptyBusPageIndex
            return read(EmptyBusPageIndex())
        with self.page_index_type(Path(self.stream.name)) as index:
            with index.covered_read(self):
                return read(index)

    def indexed_records(self, rows: Iterable[BusPageRow], *,
                        before_offset: int) -> Iterator[tuple[Message, int]]:
        """Decode selected offsets through this original descriptor and byte cut."""
        if not 0 <= before_offset <= self.boundary:
            raise RelationViolationError("Indexed read exceeds its opened wire cut")
        for row in rows:
            (message, size), = self.public_page_records(
                *row.record(self.stream, max_bytes=before_offset - row.offset),
                self.metadata,
            )
            yield message, size

    def append_checkpoint(self, index: BusAppendIndex) -> AppendCheckpoint | NoCheckpoint:
        """Validate the captured candidate only when a consumer reads this cut."""
        record = index.capture(self._append_checkpoints.get(index, NoCheckpoint()), self)
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
        return self.revision.size

    def _tail_bytes(self, max_bytes: int) -> tuple[bytes, int]:
        """Read trailing bytes from this fixed cut's retained descriptor."""
        start = max(0, self.boundary - max_bytes)
        self.stream.seek(start)
        return self.stream.read(self.boundary - start), start

    def appended_records(self, previous: FileRevision,
                         previous_sequence: int) -> tuple[WireRecord, ...]:
        """Decode only bytes appended to the same opened source inode."""
        if not self.same_file(previous.identity) or previous.size > self.revision.size:
            raise RelationViolationError("Wire tail does not extend the settled source")
        self.stream.seek(previous.size)
        scan = WireScan(self.metadata, previous_sequence=previous_sequence)
        records = [scan.read(raw) for _, raw in WireRecord.rows(self.stream, self.boundary, self.boundary)]
        if scan.previous_sequence != self.metadata.last_seq:
            raise RelationViolationError("Wire tail does not reach its marker")
        if FileRevision.from_file(self.stream) != self.revision:
            raise RelationViolationError("Wire tail changed while it was read")
        return tuple(records)

    def public_records(self, offset: int = 0, *, before_offset: int | None = None
                       ) -> Iterator[tuple[Message, int]]:
        boundary = self.boundary if before_offset is None else before_offset
        if not 0 <= offset <= boundary <= self.boundary:
            raise ValueError("Public read exceeds its opened source cut")
        self.stream.seek(offset)
        for record, raw_size in _iter_jsonl_stream(
            self.stream, boundary=boundary, label="wire snapshot"
        ):
            yield from self.public_page_records(record, raw_size, self.metadata)

@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationSourceWindowRequest:
    """One bounded indexed conversation selection from a certified wire cut."""

    limit: int
    ascending: bool
    predicate: str = "1"
    parameters: tuple = ()
    sequences: tuple[int, ...] = ()

    def capture(self, source, lookup: str) -> Iterator[CommittedDelivery]:
        """Copy selected bytes now; decode them only as the consumer advances."""
        return (
            source.conversation_sources(
                lookup,
                self.predicate,
                self.parameters,
                limit=self.limit,
                ascending=self.ascending,
                sequences=self.sequences,
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


class SnapshotOpening(ABC):
    @abstractmethod
    def capture(self, log: WireLog, resources: ExitStack): ...


class CurrentSnapshotOpening(SnapshotOpening):
    def through(self, log: WireLog, lock: StoreLock) -> int:
        return 0

    @contextmanager
    def capture(self, log: WireLog, resources: ExitStack):
        with log.locked(mode=SharedLock()) as lock:
            metadata = (
                log.private_marker_unlocked()
                if log.path.exists() or log.metadata_path.exists()
                else WireMetadata()
            )
            through = self.through(log, lock)
            opened = log._open_snapshot_unlocked(resources, metadata, through)
            if lock.source is not None:
                source = lock.certified_read()
                source.require_marker(metadata)
                source.require_open_prefix()
                opened = opened.checked(source)
        yield opened


class SequencedSnapshotOpening(CurrentSnapshotOpening):
    def through(self, log: WireLog, lock: StoreLock) -> int:
        return log.committed_sequence_unlocked(lock)


class SavedPrefixOpening(SnapshotOpening):
    """Capture bytes only; the saved checkpoint must verify its own original prefix.

    This never certifies the current tail or grants publication/input authority.
    An interrupted later append cannot invalidate an already verified prefix.
    """

    @contextmanager
    def capture(self, log: WireLog, resources: ExitStack):
        metadata = log.private_marker_unlocked()
        yield log._open_snapshot_unlocked(resources, metadata, 0)


class WireStoreLock(StoreLock):
    """The bus lock verifies its source and repairs only while held exclusively."""

    @classmethod
    @contextmanager
    def open_source(cls, path: Path, mode: LockMode):
        log = WireLog(path)
        private_marker = log._claim_marker_unlocked()
        if private_marker is None:
            log._sync_page_index_unlocked(mode)
            yield None
            return
        marker_info = log.metadata_path.lstat()
        if (
            os.name != "posix"
            or not stat.S_ISREG(marker_info.st_mode)
            or marker_info.st_uid != os.geteuid()
            or stat.S_IMODE(marker_info.st_mode) != 0o600
        ):
            raise RelationViolationError("Claim bus read barrier is not durable and private.")
        from .private_bus_checkpoint import opened_private_checkpoint_unlocked

        with opened_private_checkpoint_unlocked(log, private_marker, mode=mode) as source:
            log._sync_page_index_unlocked(mode)
            yield source


class WireLog(StoreLockOwner):
    lock_rank = 1
    lock_type = WireStoreLock
    page_index_type = BusPageIndex

    def __init__(self, path: Path):
        # Construction is read-only and needs no registry or publication settings.
        self.path = Path(path)
        self._append_indexes: list[BusAppendIndex] = []

    def register_append_index[Index: BusAppendIndex](self, index: Index) -> Index:
        """Register one index owner for automatic capture with every opened cut."""
        if index.source_path != self.path:
            raise ValueError("Append index belongs to another bus")
        if index not in self._append_indexes:
            self._append_indexes.append(index)
        return index

    @contextmanager
    def certified_read(self, *, blocking: bool = True, contention: StoreLockContention | None = None):
        """Borrow the original source verified by the canonical reader barrier."""
        with self.locked(mode=SharedLock(), blocking=blocking, contention=contention) as lock:
            with self._certified_source(lock) as source:
                yield source

    async def read_certified_async(self, read: Callable):
        """Acquire cancellably, then consume the whole source in its worker.

        Return detached observations only. Verification and the callback share
        one resource thread; no SQLite connection crosses to the event loop.
        The worker releases its locks before returning its result; result
        delivery must not keep a POSIX lock until the event loop resumes.
        Cancellation joins acquired work before the original descriptor closes.
        """
        from .private_bus_checkpoint import CheckpointNeedsRepair

        # Readers share the bus lock so idle workers don't queue behind each
        # other; only a checkpoint repair needs the exclusive lock.
        mode = SharedLock()
        try:
            return await self._read_checked_under_lock(read, mode)
        except CheckpointNeedsRepair as error:
            return await self._read_checked_under_lock(read, mode.after_repair_needed(error))

    async def _read_checked_under_lock(self, read: Callable, mode: LockMode):
        from .child_process import Platform
        from .coordinator import Coordination

        platform = Platform.current()
        with _store_lock_file(self.path) as lock_file:
            await StoreLockContention(math.inf).acquire_async(
                lock_file.fileno(), platform, mode=mode,
            )
            return await Coordination.run_worker(
                partial(self._read_certified, lock_file, platform, read, mode),
            )

    def _read_certified(self, lock_file, platform, read, mode):
        with lock_file, _held_store_source(self.path, lock_file, platform, None,
                                           mode=mode, lock_type=self.lock_type) as lock:
            with self._certified_source(lock) as source:
                return read(source)

    @contextmanager
    def _certified_source(self, lock: StoreLock):
        """Sync and async acquisition consume the same marker/currentness owner."""
        marker = self.private_marker_unlocked()
        source = lock.certified_read()
        source.require_marker(marker)
        yield source
        source.require_current()

    def read_page[T](self, request: MessageReadRequest[T]) -> T:
        """Acquire the one source borrowed by every part of a page request."""
        with self.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as source:
            return request.read_opened(source)

    def addressed_sources(self, lookup: str, after_seq: int = 0) -> Iterator[CommittedDelivery]:
        """Borrow bounded pages from one original committed append-only cut.

        A later append cannot extend this iteration. Each page captures exact
        original pointers and bytes inside certification; validation and all
        consumer work occur after releasing the bus locks.
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

    def record_context(self, manifest) -> None:
        """Append the context records chosen by the certified entry owner."""
        previous = RecordedContextManifestSources.latest(self, manifest.thread)
        with self.locked() as lock:
            marker = self.private_marker_unlocked()
            for record in lock.certified_read().context_records(manifest, previous):
                self.append_private_unlocked(marker, record.to_wire())

    def context_manifest_page(self, name: str, registry, *, cursor=None, previous=()):
        return RecordedContextManifestSources.for_owner(
            self, name, registry, cursor=cursor, previous=previous)

    @contextmanager
    def retained_sources(self, name: str, registry):
        """Borrow one certified wire/registry/input cut for retained readers.

        Capture original bytes under the existing wire/bus/registry/input cut,
        then release the bus locks before decoding. This is an observation, never
        permission to start input or compact history.
        """
        from .exporting import WireExportBoundary
        from .input_disposition import InputDispositions
        from .retained_task_facts import RetainedTaskFacts

        with (
            _store_lock(self.path.parent / "wire", mode=SharedLock()),
            self.certified_read() as source,
        ):
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

    def private_marker_unlocked(self) -> WireMetadata:
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
            for offset, line in WireRecord.rows(stream):
                try:
                    record = scan.read(line)
                except StoredHistoryUnreadable as error:
                    raise StoredHistoryUnreadable(
                        f"Unreadable bus row at {self.path}, byte offset {offset}."
                    ) from error
                if on_row is not None:
                    on_row(offset, line, record)
                yield record

    def append_private_unlocked(self, metadata: WireMetadata, row: Mapping[str, object]) -> None:
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
            except (OSError, sqlite3.Error, ValueError) as error:
                # This append owns the already-fsynced bus row. Checkpoint
                # storage or validation failure cannot mean the row is absent.
                raise RelationViolationError(
                    "Private bus checkpoint publication outcome UNKNOWN."
                ) from error
        self._sync_page_index_unlocked(ExclusiveLock())
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
        """Read checkpoint resources while the caller holds the bus locks."""
        from .private_bus_checkpoint import opened_private_checkpoint_unlocked

        marker = self.private_marker_unlocked()
        if marker.root_id != wire_root_id:
            raise RelationViolationError("Initial wire root does not match the bus marker.")
        with opened_private_checkpoint_unlocked(self, marker) as source:
            originals = tuple(source.delivery(seq) for seq in sequences)
            source.require_current()
            return originals

    def keyed_publication_unlocked(
        self, intent: PublicationIntents
    ) -> tuple[Message | None, int, WireMetadata]:
        """Resolve the original acknowledgement through the canonical sealed prefix.

        Caller holds the bus file lock. Absence is NOT authorization to append.
        """

        metadata = self.private_marker_unlocked()
        from .private_bus_checkpoint import opened_private_checkpoint_unlocked

        with opened_private_checkpoint_unlocked(self, metadata) as source:
            matched = source.keyed_receipt(intent)
            source.require_current()
            return matched, source.witness.through_seq, metadata

    def read_keyed_response(self, intent: PublicationIntents) -> Message | None:
        """Read-only exact acknowledgement resolution; never append or repair an absent row."""
        with self.certified_read() as source:
            return source.keyed_receipt(intent)

    def _prepare_append_indexes(self) -> tuple[tuple[BusAppendIndex, AppendCheckpoint | NoCheckpoint], ...]:
        """Read registered index candidates before taking any source locks."""
        return tuple((index, index.prepare()) for index in self._append_indexes)

    @contextmanager
    def opened_wire_snapshot(
        self, *,
        opening: SnapshotOpening = SequencedSnapshotOpening(),
    ):
        """Capture registered indexes before opening their original source cut."""
        prepared = self._prepare_append_indexes()
        with ExitStack() as resources:
            with opening.capture(self, resources) as opened:
                opened.capture_append_indexes(prepared)
                yield opened

    def _open_snapshot_unlocked(self, resources: ExitStack, metadata: WireMetadata, through: int) -> WireSnapshot:
        """The sole descriptor acquisition and absent/present snapshot selection."""
        try:
            stream = resources.enter_context(self.path.open("rb"))
        except FileNotFoundError:
            return AbsentWireSnapshot(metadata, through)
        return OpenedWireSnapshot(metadata, through, stream, FileRevision.from_file(stream), self.page_index_type)

    @contextmanager
    def verified_snapshot(self) -> Iterator[Iterator[WireRecord]]:
        """Run the strict WireScan after releasing the bus locks.

        Uncertified streams retain the same complete validation algorithm.
        This resource is a fixed read, not permission to append or start input.
        """
        with self.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as opened:
            yield opened.records()

    @contextmanager
    def _record_snapshot(self) -> Iterator[tuple[int, Iterator[tuple[Message, int]]]]:
        """Explicit sequential history borrows the original opened byte boundary."""
        with self.opened_wire_snapshot() as opened:
            yield opened.through, opened.public_records()

    @contextmanager
    def full_history_snapshot(self) -> Iterator[tuple[int, Iterator[Message]]]:
        """Open one fixed append boundary without retaining the complete wire."""
        with self._record_snapshot() as (through, records):
            yield through, (message for message, _ in records)

    def messages_for_references(self, references):
        with self.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as opened:
            return opened.messages_for_references(references)

    def deliveries_for_references(self, references):
        """One canonical lock/certificate lifetime for a visible source window."""
        if not references:
            return ()
        with self.certified_read() as source:
            originals = source.references(references)
        return tuple(originals)

    def latest_sequence(self) -> int:
        """Return the global high-water sequence without loading message bodies."""
        with self.locked(mode=SharedLock()) as lock:
            return self.committed_sequence_unlocked(lock)

    def committed_sequence_unlocked(self, lock: StoreLock) -> int:
        """Read the committed cut from the resource acquired by this barrier.

        The certificate's global through_seq is not the addressed page's
        latest_source_seq or a marker reservation. The existing read barrier
        refuses private claim streams without their required certificate.
        """
        if lock.source is not None:
            return lock.certified_read().committed_sequence()
        return self.read_metadata_unlocked().last_seq

    def _claim_marker_unlocked(self) -> WireMetadata | None:
        metadata = self.read_metadata_unlocked()
        from .private_bus_checkpoint import certificate_enabled

        if certificate_enabled(self.path) and not metadata.claims:
            raise RelationViolationError("Private checkpoint lacks its claim read barrier.")
        return metadata if metadata.claims else None

    def _sync_page_index_unlocked(self, mode: LockMode) -> None:
        """Lend readers only indexed cuts; missing or stale coverage needs a writer."""
        with ExitStack() as resources:
            opened = self._open_snapshot_unlocked(resources, self.private_marker_unlocked(), 0)
            if not opened.complete_prefix.boundary:
                return
            with self.page_index_type(self.path) as index:
                if not index.covers(opened):
                    mode.require_writer()
                    index.sync(opened)

    @property
    def metadata_path(self) -> Path:
        return self.path.with_name("bus_meta.json")

    def read_metadata_unlocked(self, *, required: bool = False) -> WireMetadata:
        """Read one marker inode under its leaf lock, independently of registry reads.

        Callers still own any required bus/registry transaction. Marker ownership
        is checked on the opened inode before this lock lets publication retire
        it; zero or multiple links are never accepted as owner-only storage.
        """
        with _store_lock(self.metadata_path, mode=SharedLock()):
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
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise RelationViolationError("Bus protocol marker must be regular storage.")
                try:
                    data = FieldCodec.parse(source.read())
                except MalformedRecord as error:
                    raise RelationViolationError("Bus protocol marker is malformed.") from error
            try:
                metadata = FieldCodec.decode(WireMetadata, data)
                # Omitted optional fields express absence. Explicit nulls, missing
                # required public sequence and mixed protocol rows are not aliases.
                if FieldCodec.encode(metadata) != data:
                    raise MalformedRecord("Noncanonical bus protocol marker")
            except MalformedRecord as error:
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
