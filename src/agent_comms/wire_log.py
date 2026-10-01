"""Canonical append-only wire persistence and durable receipt authority."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from agent_comms.coordination_tables.publications import (
    PublicationIntents,
)

from .bus_publication import (
    CommittedDelivery,
    stable_thread_lookup,
    has_private_wire_fields,
    unique_wire_object,
)
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
    _atomic_write_text,
    _iter_jsonl_records,
    _iter_jsonl_stream,
    _store_lock,
)
from .wire_metadata import WireMetadata
from .wire_record import WireRecord, WireScan

if TYPE_CHECKING:

    from .thread_identity import ThreadIncarnation


class WireLog:
    def __init__(self, path: Path):
        # Construction is read-only and needs no registry or publication settings.
        self.path = Path(path)

    @contextmanager
    def locked(self, *, blocking: bool = True, max_bus_bytes: int | None = None):
        """The existing canonical bus lock and durability read barrier."""
        with _store_lock(self.path, blocking=blocking, max_bus_bytes=max_bus_bytes) as lock:
            yield lock

    @contextmanager
    def certified_read(self, *, blocking: bool = True):
        """Borrow the original source verified by this canonical lock barrier."""
        with _store_lock(self.path, blocking=blocking) as lock:
            marker = self._private_marker_unlocked()
            source = lock.certified_read()
            source.require_marker(marker)
            yield source
            source.require_current()

    def conversation_sources(self, lookup, predicate, parameters, *, limit, ascending):
        """Read an original window through its existing canonical barrier."""
        from .private_bus_checkpoint import conversation_sources_unlocked

        with _store_lock(self.path) as lock:
            if not self.path.exists():
                return ()
            marker = self._private_marker_unlocked()
            source = lock.certified_read()
            source.require_marker(marker)
            originals = conversation_sources_unlocked(
                source, lookup, predicate, parameters, limit=limit, ascending=ascending
            )
            source.require_current()
            return originals

    def full_history(self) -> list[Message]:
        with self.locked():
            return list(self._iter_log_unlocked())

    def record_context(self, manifest) -> None:
        """Append one text-free observation through the original sealed writer."""
        from .wire_record import ContextManifestWireObservation, ObservationWireRecord

        with self.locked():
            marker = self._private_marker_unlocked()
            record = ObservationWireRecord(ContextManifestWireObservation(manifest))
            self._append_private_unlocked(marker, record.to_wire())

    def context_manifests(self, incarnation):
        """Observe original rows; no context sidecar, receipt or sequence index."""
        with self.locked():
            marker = self._private_marker_unlocked()
            return tuple(
                manifest
                for record in self.verified_records_unlocked(marker)
                for manifest in record.context_manifests()
                if manifest.thread == incarnation
            )

    def compaction_messages_unlocked(self, recipient: ThreadIncarnation):
        """One strict wire traversal supplies both the source cut and exact facts.

        Caller owns the original bus lock. Outgoing declared decisions belong
        to their author's source too; unrelated messages cannot invalidate it.
        """
        digest = hashlib.sha256()
        facts = []
        lookup = stable_thread_lookup(recipient.created_at)
        marker = self._private_marker_unlocked()
        for record in self.verified_records_unlocked(marker):
            for message in record.compaction_messages_for(lookup):
                digest.update(json.dumps(FieldCodec.encode(message), sort_keys=True).encode())
                digest.update(b"\n")
                facts.extend(message.retained_task_facts())
        return digest.hexdigest(), tuple(facts)

    def retained_context(self, name: str, registry):
        """One certified source cut for read-only context inspection/export."""
        from .exporting import WireExportBoundary
        from .retained_context import RetainedSegment
        from .retained_task_facts import RetainedTaskFacts
        from .turn_context import OwnerProvenance

        with _store_lock(self.path.parent / "wire"), self.locked():
            snapshot = registry.snapshot()
            owner = snapshot.require(name)
            digest, facts = self.compaction_messages_unlocked(owner.incarnation)
            retained = RetainedTaskFacts(facts).for_owner(owner, snapshot)
            return RetainedSegment.capture(retained, OwnerProvenance(owner.incarnation, digest),
                owner, snapshot, WireExportBoundary(self._private_marker_unlocked().last_seq, time.time()))

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
        projection = ClaimProjection()
        verified_sequence = 0
        for record in self.verified_records_unlocked(metadata):
            verified_sequence = record.sequence_after(verified_sequence)
            for previous in record.messages():
                if previous.claim_transition is not None:
                    projection = apply_transition(projection, previous.claim_transition)
        return projection, verified_sequence

    def claim_projection(self) -> ClaimProjection:
        """Derive ownership exclusively from guarded, verified bus envelopes."""
        with _store_lock(self.path):
            metadata = self._private_marker_unlocked()
            if not metadata.claims:
                raise RelationViolationError("Claim read barrier is unavailable.")
            projection, _verified_sequence = self._claim_projection_unlocked(metadata)
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
            self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600
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
        directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
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

    @staticmethod
    def _public_page_records(
        record: Mapping, raw_size: int, metadata: WireMetadata
    ) -> Iterator[tuple[Message, int]]:
        """Silent facts consume physical bytes, never message/display budgets."""
        for message in WireRecord.public_from_wire(record).messages():
            if has_private_wire_fields(record):
                yield message, len(json.dumps(message.to_wire()).encode()) + 1
            else:
                message.require_retained_admission(metadata.admission_after_seq)
                yield message, raw_size

    @contextmanager
    def _record_snapshot(
        self, *, need_sequence: bool = True
    ) -> Iterator[tuple[int, Iterator[tuple[Message, int]]]]:
        """Fixed opened-inode/byte boundary with public page-size accounting.

        Display-only callers do not request a sequence watermark. Every stored
        source has the current marker; history never repairs an absent marker.
        """
        with _store_lock(self.path):
            metadata = (
                self._private_marker_unlocked()
                if self.path.exists() or self.metadata_path.exists()
                else WireMetadata()
            )
            if need_sequence and self.claim_gate_enabled():
                # Metadata reserves a sequence BEFORE the append. A failed
                # append must never surface as a committed message watermark.
                through = self._max_sequence_unlocked()
            else:
                through = metadata.last_seq if need_sequence else 0
            try:
                stream: BinaryIO | None = self.path.open("rb")
            except FileNotFoundError:
                stream = None
            boundary = stream.seek(0, 2) if stream is not None else 0
            if stream is not None:
                stream.seek(0)
        try:
            records = (
                (
                    page_row
                    for record, size in _iter_jsonl_stream(
                        stream, boundary=boundary, label="wire snapshot"
                    )
                    for page_row in self._public_page_records(record, size, metadata)
                )
                if stream is not None
                else iter(())
            )
            yield through, records
        finally:
            if stream is not None:
                stream.close()

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
        from .private_bus_checkpoint import delivery_references_unlocked

        if not references:
            return ()
        with self.certified_read() as source:
            return delivery_references_unlocked(source, references)

    def total_messages(self) -> int:
        with _store_lock(self.path):
            return sum(1 for _ in self._iter_log_unlocked())

    def latest_sequence(self) -> int:
        """Return the global high-water sequence without loading message bodies."""
        with _store_lock(self.path):
            if self.claim_gate_enabled():
                return self._max_sequence_unlocked()
            return self.read_metadata_unlocked().last_seq

    def _iter_log_unlocked(self) -> Iterator[Message]:
        if self.path.exists():
            marker = self._private_marker_unlocked()
            for record in self.verified_records_unlocked(marker):
                yield from record.messages()

    def _max_sequence_unlocked(self) -> int:
        return max(
            (message.seq for message in self._iter_log_unlocked()),
            default=0,
        )

    def claim_gate_enabled(self) -> bool:
        # _store_lock is also used for registry, channels, and marker files.
        # Only the canonical bus may enter this read/durability barrier.
        if self.path.name != "bus.jsonl":
            return False
        metadata = self.read_metadata_unlocked()
        from .private_bus_checkpoint import certificate_enabled

        if certificate_enabled(self.path) and not metadata.claims:
            raise RelationViolationError("Private checkpoint lacks its claim read barrier.")
        return metadata.claims

    @contextmanager
    def verify_before_read_unlocked(self):
        """Make every visible opt-in bus row durable before ANY bus-lock reader sees it.

        The marker is fsynced before the first claim send. A failed bus append may
        leave a complete, visible row: another cooperating process must fsync the
        opened inode and its directory, then reject incomplete/corrupt rows, before
        treating either the announcement or the claim as committed. This hook is
        entered by the shared bus lock, including ordinary inbox/history readers.
        """
        if not self.claim_gate_enabled():
            yield None
            return
        private_marker = self.read_metadata_unlocked()
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

        with opened_claim_source_unlocked(self, private_marker) as source:
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
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
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
