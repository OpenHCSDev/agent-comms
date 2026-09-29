"""Canonical append-only wire persistence and durable receipt authority."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from agent_comms.coordination_tables.publications import (
    PublicationIntents,
)

from .bus_publication import (
    PRIVATE_WIRE_FIELD,
    CommittedDelivery,
    has_private_wire_fields,
    public_envelope_digest,
    unique_wire_object,
    validate_delivery_record,
)
from .delivery_policy import KeyedResponseReceipt
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
    file_revision,
)
from .wire_metadata import WireMetadata

if TYPE_CHECKING:

    from .routing import DeliveryScope


class WireLog:
    def __init__(self, path: Path):
        # Construction is read-only and needs no registry or publication settings.
        self.path = Path(path)

    @contextmanager
    def locked(self, *, blocking: bool = True, max_bus_bytes: int | None = None):
        """The existing canonical bus lock and durability read barrier."""
        with _store_lock(self.path, blocking=blocking, max_bus_bytes=max_bus_bytes) as descriptor:
            yield descriptor

    def full_history(self) -> list[Message]:
        with self.locked():
            return list(self._iter_log_unlocked())

    def delivery_revision_unlocked(self, delivery: DeliveryScope) -> str:
        """Hash the owner's ingress while retaining only one wire record.

        Caller holds the canonical wire lock. The revision deliberately excludes
        unrelated deliveries; all records still undergo sequence validation.
        """
        digest = hashlib.sha256()
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return digest.hexdigest()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RelationViolationError("Compaction ingress must be regular storage")
        previous = 0
        selected = False
        try:
            with self.path.open("rb") as stream:
                for raw in stream:
                    if not raw.endswith(b"\n"):
                        raise ValueError("Incomplete bus row")
                    line = raw.removesuffix(b"\n").removesuffix(b"\r")
                    message = Message.from_wire(
                        json.loads(line, object_pairs_hook=unique_wire_object)
                    )
                    if message.seq <= previous:
                        raise ValueError("Bus sequence is not increasing")
                    previous = message.seq
                    if delivery.delivers(message.sender, message.target):
                        if selected:
                            digest.update(b"\n")
                        digest.update(line)
                        selected = True
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise RelationViolationError("Invalid compaction ingress bus") from error
        return digest.hexdigest()

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
        for previous, _, _ in self._verified_private_rows_unlocked(metadata):
            verified_sequence = previous.seq
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

    def _verified_private_rows_unlocked(
        self,
        metadata: WireMetadata,
        *,
        on_row: (
            Callable[
                [int, bytes, Message, KeyedResponseReceipt | None, CommittedDelivery | None], None
            ]
            | None
        ) = None,
    ) -> Iterator[tuple[Message, KeyedResponseReceipt | None, CommittedDelivery | None]]:
        """Validate the ENTIRE append-only log before any new append or trusted read.

        A later corrupt row cannot be skipped to attest an earlier row. No
        incomplete tail or quarantine is silently discarded on the private path.
        """

        from .audience_manifest import MAX_WIRE_SEQ
        from .bus_publication import _canonical

        previous_sequence = 0
        seen_keys: set[str] = set()
        if not self.path.exists():
            return
        with self.path.open("rb") as stream:
            while True:
                offset = stream.tell()
                line = stream.readline(8 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 8 * 1024 * 1024:
                    raise RelationViolationError("Oversized private bus row.")
                if not line.endswith(b"\n"):
                    raise RelationViolationError("Incomplete bus row blocks keyed publication.")
                try:
                    record = json.loads(line, object_pairs_hook=unique_wire_object)
                    if not isinstance(record, dict):
                        raise ValueError("Bus row is not an object.")
                    existing = Message.from_wire(record)
                    public = existing.to_wire()
                    raw_public = {
                        key: value for key, value in record.items() if key != PRIVATE_WIRE_FIELD
                    }
                    if (
                        type(record.get("seq")) is not int
                        or not previous_sequence < record["seq"]
                        or record["seq"] > MAX_WIRE_SEQ  # never cap by a stale marker hint
                        or any(
                            type(record.get(field)) is not str
                            for field in ("id", "from", "to", "text", "type", "sender_role")
                        )
                        or _canonical(raw_public) != _canonical(public)
                    ):
                        raise ValueError("Noncanonical public bus envelope or sequence.")
                    public_envelope_digest(public)
                    previous_sequence = existing.seq
                    if not has_private_wire_fields(record):
                        if (
                            existing.claim_transition is None
                            and existing.seq > metadata.admission_after_seq
                        ):
                            raise RelationViolationError(
                                "Unattested public initial exceeds the retained history boundary."
                            )
                        if on_row is not None:
                            on_row(offset, line, existing, None, None)
                        yield existing, None, None
                        continue
                    if set(key for key in record if key.startswith("_agent_comms_private")) != {
                        PRIVATE_WIRE_FIELD
                    }:
                        raise ValueError("Unknown private bus namespace.")
                    private = record[PRIVATE_WIRE_FIELD]
                    if (
                        not isinstance(private, dict)
                        or type(private.get("version")) is not int
                        or private["version"] != 1
                    ):
                        raise ValueError("Unsupported private bus record.")
                    initial = validate_delivery_record(record, metadata.root_id)
                    receipt = initial.receipt
                    if receipt is not None:
                        receipt.add_unique(seen_keys)
                    if on_row is not None:
                        on_row(offset, line, existing, receipt, initial)
                    yield existing, receipt, initial
                except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
                    if isinstance(error, RelationViolationError):
                        raise
                    if "Duplicate bus object key" in str(error):
                        raise
                    raise RelationViolationError(
                        "Malformed public bus row blocks publication."
                    ) from error

    def _append_private_unlocked(self, metadata: WireMetadata, row: Mapping[str, object]) -> None:
        """Durably reserve a sequence, append one row, then sync its parent.

        A failed append or bus parent sync leaves the outcome UNKNOWN.
        """
        metadata.access.require_append()
        encoded = json.dumps(row, allow_nan=False).encode("utf-8") + b"\n"
        if len(encoded) > 8 * 1024 * 1024:
            raise RelationViolationError("Private bus row exceeds the byte limit.")
        metadata.last_seq = row["seq"]  # type: ignore[assignment]
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
            private = row.get(PRIVATE_WIRE_FIELD)
            initial = (
                validate_delivery_record(row, metadata.root_id)
                if isinstance(private, dict) and "initial" in private
                else None
            )
            receipt = initial.receipt if initial is not None else None
            public = {key: value for key, value in row.items() if key != PRIVATE_WIRE_FIELD}
            try:
                append_private_bus_checkpoint_unlocked(
                    self, metadata, encoded, Message.from_wire(public), receipt, initial
                )
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
        with _store_lock(self.path):
            metadata = self._private_marker_unlocked()
            if wire_root_id != metadata.root_id:
                raise RelationViolationError("Initial wire root does not match the bus marker.")
            matched: CommittedDelivery | None = None
            for _message, _receipt, initial in self._verified_private_rows_unlocked(metadata):
                if initial is not None and initial.message.seq == wire_seq:
                    matched = initial
            if matched is None:
                raise RelationViolationError(
                    "No committed initial sideband for this wire sequence."
                )
            return matched

    def _keyed_receipt_unlocked(
        self, intent: PublicationIntents
    ) -> tuple[Message | None, int, WireMetadata]:
        """Read the whole owner-only bus before trusting an exact keyed receipt.

        Caller holds the bus file lock. Absence is NOT authorization to append.
        """

        if type(intent) is not PublicationIntents:
            raise TypeError("Keyed response requires a validated PublicationIntents.")
        metadata = self._private_marker_unlocked()
        matched: Message | None = None
        previous_sequence = 0
        for existing, receipt, _initial in self._verified_private_rows_unlocked(metadata):
            previous_sequence = existing.seq
            if receipt is not None and receipt.publication_key == intent.publication_key:
                if receipt.execution_id != intent.execution_id:
                    raise RelationViolationError("Response publication identity conflicts.")
                matched = existing
        if matched is not None:
            expected = intent.expected_message
            if (
                matched.sender != intent.sender
                or matched.target != intent.exact_target
                or matched.body != expected.body
                or matched.type is not expected.type
                or matched.timestamp != expected.timestamp
                or matched.notice != expected.notice
                or matched.membership != expected.membership
                or matched.message_id != intent.expected_message_id
            ):
                raise RelationViolationError("Response publication intent conflicts.")
        return matched, previous_sequence, metadata

    def read_keyed_response(self, intent: PublicationIntents) -> Message | None:
        """Read-only exact receipt resolution; never append or repair an absent row."""
        with _store_lock(self.path):
            matched, _, _ = self._keyed_receipt_unlocked(intent)
            return matched

    @staticmethod
    def _public_page_record(
        record: Mapping, raw_size: int, metadata: WireMetadata
    ) -> tuple[Message, int]:
        """Charge public page budgets for public bytes, never private sidebands."""
        message = Message.from_wire(record)
        if has_private_wire_fields(record):
            return message, len(json.dumps(message.to_wire()).encode()) + 1
        if message.claim_transition is None and message.seq > metadata.admission_after_seq:
            raise RelationViolationError(
                "Unattested public initial exceeds the retained history boundary."
            )
        return message, raw_size

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
                    self._public_page_record(record, size, metadata)
                    for record, size in _iter_jsonl_stream(
                        stream, boundary=boundary, label="wire snapshot"
                    )
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

    @staticmethod
    @lru_cache(maxsize=4)
    def _receipt_offsets(
        path: Path, revision: tuple[int, int, int, int] | None
    ) -> Mapping[str, int]:
        offsets: dict[str, int] = {}
        if revision is None:
            return offsets
        with path.open("rb") as stream:
            while True:
                offset = stream.tell()
                raw = stream.readline()
                if not raw:
                    break
                try:
                    record = json.loads(raw)
                except ValueError:
                    if not raw.endswith(b"\n"):
                        break
                    raise
                if isinstance(record, dict) and isinstance(record.get("id"), str):
                    offsets[record["id"]] = offset
        return offsets

    def message_by_id(self, message_id: str) -> Message | None:
        """Look up a durable receipt without retaining the wire's message bodies."""
        with _store_lock(self.path):
            offset = self._receipt_offsets(self.path, file_revision(self.path)).get(message_id)
            if offset is None:
                return None
            with self.path.open("rb") as stream:
                stream.seek(offset)
                return Message.from_wire(json.loads(stream.readline()))

    def total_messages(self) -> int:
        with _store_lock(self.path):
            return sum(1 for _ in _iter_jsonl_records(self.path))

    def latest_sequence(self) -> int:
        """Return the global high-water sequence without loading message bodies."""
        with _store_lock(self.path):
            if self.claim_gate_enabled():
                return self._max_sequence_unlocked()
            return self.read_metadata_unlocked().last_seq

    def _iter_log_unlocked(self) -> Iterator[Message]:
        if self.path.exists():
            marker = self._private_marker_unlocked()
            for message, _receipt, _initial in self._verified_private_rows_unlocked(marker):
                yield message

    def _max_sequence_unlocked(self) -> int:
        return max(
            (int(record.get("seq", 0)) for record, _ in _iter_jsonl_records(self.path)),
            default=0,
        )

    def _last_row_sequence_unlocked(self) -> int:
        """Read the final complete row without rescanning the whole bus on send."""
        try:
            with self.path.open("rb") as records:
                records.seek(0, os.SEEK_END)
                end = records.tell()

                def previous_newline(before: int) -> int:
                    cursor = before
                    while cursor:
                        start = max(0, cursor - 64 * 1024)
                        records.seek(start)
                        offset = records.read(cursor - start).rfind(b"\n")
                        if offset >= 0:
                            return start + offset
                        cursor = start
                    return -1

                last_newline = previous_newline(end)
                if last_newline < 0:
                    return 0  # An incomplete first row is repaired before append.
                prior_newline = previous_newline(last_newline)
                records.seek(prior_newline + 1)
                raw = records.read(last_newline - prior_newline - 1)
        except FileNotFoundError:
            return 0
        try:
            row = json.loads(raw, object_pairs_hook=unique_wire_object)
            if type(row) is not dict or type(row.get("seq")) is not int or row["seq"] < 1:
                raise ValueError("Invalid last bus sequence")
            return int(row["seq"])
        except (ValueError, UnicodeError) as error:
            raise RelationViolationError("Malformed last bus row blocks publication.") from error

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

    def verify_before_read_unlocked(self) -> None:
        """Make every visible opt-in bus row durable before ANY bus-lock reader sees it.

        The marker is fsynced before the first claim send. A failed bus append may
        leave a complete, visible row: another cooperating process must fsync the
        opened inode and its directory, then reject incomplete/corrupt rows, before
        treating either the announcement or the claim as committed. This hook is
        entered by the shared bus lock, including ordinary inbox/history readers.
        """
        if not self.claim_gate_enabled():
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
        try:
            # The private marker's source of truth is its single fsynced JSON file.
            # The root identity/sequence/claim flag are validated again by private
            # writers; this preflight protects all ordinary readers on marked roots.
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.path, flags)
            except FileNotFoundError:
                descriptor = None
            from .private_bus_checkpoint import certificate_enabled

            if descriptor is None and (
                certificate_enabled(self.path) or private_marker.checkpoint_seal is not None
            ):
                raise RelationViolationError("Private checkpoint bus inode is missing.")
            if descriptor is not None:
                with os.fdopen(descriptor, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise RelationViolationError("Claim bus is not a regular file.")
                    os.fsync(stream.fileno())
                    from .private_bus_checkpoint import (
                        certificate_enabled,
                        verify_private_bus_checkpoint_unlocked,
                    )

                    if certificate_enabled(self.path) or private_marker.checkpoint_seal is not None:
                        private_marker = self._private_marker_unlocked()
                        if private_marker.checkpoint_seal is None:
                            raise RelationViolationError(
                                "Private checkpoint lacks durable marker binding."
                            )
                        directory_fd = os.open(
                            self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                        )
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
                        verify_private_bus_checkpoint_unlocked(self, private_marker)
                        return
                    while line := stream.readline(8 * 1024 * 1024 + 1):
                        if len(line) > 8 * 1024 * 1024 or not line.endswith(b"\n"):
                            raise RelationViolationError("Incomplete or oversized claim bus row.")
                        try:
                            row = json.loads(line, object_pairs_hook=unique_wire_object)
                        except (ValueError, UnicodeError) as error:
                            raise RelationViolationError("Malformed claim bus row.") from error
                        if not isinstance(row, dict):
                            raise RelationViolationError("Claim bus row must be an object.")
                        if "claim_transition" in row:
                            try:
                                public = Message.from_wire(row).to_wire()
                            except (KeyError, TypeError, ValueError, AttributeError) as error:
                                raise RelationViolationError("Malformed claim envelope.") from error
                            if {
                                key: value
                                for key, value in row.items()
                                if key != PRIVATE_WIRE_FIELD
                            } != public:
                                raise RelationViolationError("Noncanonical claim envelope.")
            directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            raise RelationViolationError("Claim bus durability is UNKNOWN.") from error

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
