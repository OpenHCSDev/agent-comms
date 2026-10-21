"""Move historical context rows once; retain every message and sealed claim row."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from abc import abstractmethod
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, BinaryIO, ClassVar, Literal

from agent_comms.context_store import (
    AbsentContext,
    ContextEntry,
    ContextStore,
    ContextStoreInfo,
    RecordedContextState,
)
from agent_comms.coordination_database import CoordinationStore
from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, Id128Text, SingleFieldValue, WireName
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import DurableFile, SQLiteFile
from agent_comms.private_path import FileRevision, PrivateFileRole
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.turn_context import (
    ContextEntryManifest,
    ContextEntryReference,
    ContextManifest,
    RecordedContextManifest,
    SegmentManifest,
)
from agent_comms.typed_table import Column, Index, TextStorage, TypedTable

from .carry_bus_history import BusHistory, HistoryMetadata


@dataclass(frozen=True)
class ContextRootId(SingleFieldValue):
    value: Annotated[str, Id128Text]


@dataclass(frozen=True)
class ContextMarker(HistoryMetadata):
    """Only the original root and sequence boundary participate in this conversion."""

    root_id: Annotated[ContextRootId, WireName("wire_root_id")]


class ContextCheckpoint(DurableFile, SQLiteFile):
    """The retired file's location and read-only snapshot, not a runtime index."""

    filename = "private_bus_checkpoint.sqlite3"

    @classmethod
    @contextmanager
    def opening(cls, root: Path) -> Iterator[sqlite3.Connection]:
        path = cls.path_in(root)
        Carry.require_confined(root, path)
        PrivateFileRole.require(path.lstat())
        with closing(CoordinationStore.read_snapshot(path)) as db:
            yield db


@dataclass(frozen=True)
class ContextPrefix(TypedTable, declared_name="prefix_certificate"):
    """Read only the old certificate fields needed to prove the complete input."""

    singleton: Annotated[Literal[1], Column(primary_key=True)]
    version: Literal[2]
    root_id: Annotated[ContextRootId, Column(storage=TextStorage)]
    offset: int
    through_seq: int
    digest: str
    tail: str

    seed: ClassVar[bytes] = hashlib.sha256(b"agent-comms:private-bus-prefix:v1\0").digest()
    tail_bytes: ClassVar[int] = 4096
    max_row_bytes: ClassVar[int] = 8 * 1024 * 1024

    @classmethod
    def read_prefix(cls, db: sqlite3.Connection) -> ContextPrefix:
        row = cls.one(db, singleton=1)
        if row is None:
            raise RelationViolationError("Historical context checkpoint has no prefix certificate")
        return row

    def require_source(self, marker: ContextMarker, stream: BinaryIO) -> None:
        if (
            self.root_id != marker.root_id
            or self.through_seq != marker.last_seq
            or self.offset != os.fstat(stream.fileno()).st_size
        ):
            raise RelationViolationError("Historical context checkpoint is not the complete bus")
        stream.seek(max(0, self.offset - self.tail_bytes))
        if hashlib.sha256(stream.read(min(self.tail_bytes, self.offset))).hexdigest() != self.tail:
            raise RelationViolationError("Historical context checkpoint prefix tail changed")

    def rows(self, stream: BinaryIO) -> Iterator[ContextLine]:
        stream.seek(0)
        digest = self.seed
        while (offset := stream.tell()) < self.offset:
            raw = stream.readline(min(self.max_row_bytes + 1, self.offset - offset))
            if len(raw) > self.max_row_bytes or not raw.endswith(b"\n"):
                raise RelationViolationError(
                    "Historical context bus has an incomplete/oversized row"
                )
            digest = hashlib.sha256(digest + len(raw).to_bytes(8, "big") + raw).digest()
            yield ContextLine(offset, raw)
        if digest.hex() != self.digest:
            raise RelationViolationError("Historical context bus differs from its proved prefix")


@dataclass(frozen=True)
class ContextLine:
    offset: int
    raw: bytes

    def move(self, move: ContextMove, output: BinaryIO) -> None:
        value = FieldCodec.decode(dict[str, object], FieldCodec.parse(self.raw))
        if FieldCodec.record_key(ContextEnvelope, "observation") in value:
            FieldCodec.decode(ContextEnvelope, value).observation.move(move, self.offset)
        else:
            # Opaque retained messages and sealed claims keep every original byte.
            output.write(self.raw)


# This old declaration exists only at this one-time format boundary: its table marks a bus
# whose context records still sit in the log.
@dataclass(frozen=True)
class RecordedContextManifestSources(TypedTable):
    offset: Annotated[int, Column(check="offset>=0")]
    length: Annotated[int, Column(check="length>0")]
    thread: ThreadIncarnation
    indexes = (Index(("thread", "offset")), Index(("offset",), unique=True))


class ContextObservation(DeclaredFamily, affix="Observation"):
    @abstractmethod
    def move(self, move: ContextMove, offset: int) -> None: ...

    def recorded(self) -> RecordedContextManifest:
        raise RelationViolationError("Context predecessor is not a recorded request")


@dataclass(frozen=True)
class ContextEntryManifestObservation(ContextObservation):
    entries: tuple[ContextEntryManifest, ...]

    def move(self, move: ContextMove, offset: int) -> None:
        for entry in self.entries:
            move.entry(entry)


@dataclass(frozen=True)
class RecordedContextManifestObservation(ContextObservation):
    manifest: RecordedContextManifest

    def recorded(self) -> RecordedContextManifest:
        return self.manifest

    def move(self, move: ContextMove, offset: int) -> None:
        move.request(self.manifest, offset)


@dataclass(frozen=True)
class ContextEnvelope:
    observation: ContextObservation
    strict_fields = True


class ContextMove:
    """Decode old positions once; the context store owns recording and predecessors.

    Rows arrive in log order, so every predecessor and every entry a request names
    was resolved earlier in the same scan; each is kept by its offset or reference
    and never read back from the log or the store."""

    def __init__(self, store: ContextStore) -> None:
        self.store = store
        with store.writing() as db:
            ContextStoreInfo.source_in(db).initialized(db)
        self.entries: dict[ContextEntryReference, SegmentManifest] = {}
        self.requests: dict[int, RecordedContextManifest] = {}
        self.values: dict[
            int, tuple[tuple[SegmentManifest, tuple[ContextEntryReference, ...]], ...]
        ] = {}
        self.priors: dict[ThreadIncarnation, RecordedContextState] = {}
        self.count = 0

    def entry(self, entry: ContextEntryManifest) -> None:
        # The first value of a journal identity wins, as insert_if_absent keeps it.
        if entry.reference not in self.entries:
            self.entries[entry.reference] = entry.manifest
            with self.store.writing() as db:
                ContextEntry.capture(entry).insert_if_absent(db)

    def original(self, reference: ContextEntryReference) -> SegmentManifest:
        if reference not in self.entries:
            raise RelationViolationError("Recorded context names an entry the log never held")
        return self.entries[reference]

    def value(
        self,
        record: RecordedContextManifest,
        position: int,
        offset: int,
    ) -> tuple[SegmentManifest, tuple[ContextEntryReference, ...]]:
        part = record.segments[position]
        header, references = part.manifest, part.entries
        for predecessor in part.predecessors(offset):
            if predecessor not in self.requests:
                raise RelationViolationError("Context predecessor is not a recorded request")
            if self.requests[predecessor].thread != record.thread:
                raise RelationViolationError("Context predecessor belongs to another owner")
            old, old_references = self.values[predecessor][position]
            if part.inherited_entries > len(old_references):
                raise RelationViolationError("Recorded context inherited entries are incomplete")
            references = (*old_references[: part.inherited_entries], *references)
            if not header.captured_text and old.sha256 == header.sha256:
                header = replace(header, captured_text=old.captured_text)
        return header, tuple(references)

    def request(self, record: RecordedContextManifest, offset: int) -> None:
        values = tuple(
            self.value(record, position, offset) for position in range(len(record.segments))
        )
        self.requests[offset], self.values[offset] = record, values
        segments = tuple(
            replace(header, contributors=tuple(self.original(ref) for ref in references))
            if references
            else header
            for header, references in values
        )
        self.priors[record.thread] = self.store.record_after(
            self.priors.get(record.thread, AbsentContext()),
            ContextManifest(
                record.thread, record.turn, segments, record.counter, record.request_id
            ),
        )
        self.count += 1


class ContextManifests(Carry):
    staging_reserve_bytes: ClassVar[int] = 1024**3

    def backup_files(self) -> tuple[Path, ...]:
        root = self.root
        return (
            root / BusHistory.filename,
            root / ContextMarker.filename,
            ContextCheckpoint.path_in(root),
            ContextStore.path_in(root),
            *self.derived_files(root),
        )

    @classmethod
    def derived_files(cls, root: Path) -> tuple[Path, ...]:
        # Exact old disposable formats, never sessions, annotations or claims.
        patterns = (
            "bus_page_index.sqlite3*",
            "wake_candidates.sqlite3*",
            "bus_route_counts.v*.sqlite3*",
            "bus_activity_*.json",
            "bus_display_*.json",
        )
        return tuple(path for pattern in patterns for path in root.glob(pattern))

    def carry(self) -> Carried:
        root = self.root
        path = ContextCheckpoint.path_in(root)
        self.require_confined(root, path)
        if not path.exists():
            return Carried(0)
        with ContextCheckpoint.opening(root) as db:
            if not RecordedContextManifestSources.present(db):
                return Carried(0)
            count = RecordedContextManifestSources.count(db)
        if not self.apply:
            return Carried(count + 1)
        return self._rewrite(root)

    @classmethod
    def _rewrite(cls, root: Path) -> Carried:
        """Convert the selected stopped root under Carry's existing wire lock.

        Prove the old format without importing its deleted runtime. Retain message
        bytes, move context observations into their existing store, and retire the
        obsolete index. The later BusHistory carry owns the new bus's indexes; no
        intermediate private checkpoint is rebuilt.
        """
        source = root / BusHistory.filename
        cls.require_confined(root, source)
        index_path = ContextCheckpoint.path_in(root)
        required = source.stat().st_size + index_path.stat().st_size + cls.staging_reserve_bytes
        filesystem = os.statvfs(root)
        available = filesystem.f_bavail * filesystem.f_frsize
        if required > available:
            raise OSError(
                f"Context conversion requires {required} bytes; "
                f"available {available} bytes on {root}"
            )
        if ContextStore.path_in(root).exists():
            raise RelationViolationError("Context conversion needs an absent destination store")
        marker = ContextMarker.read(root)
        with ContextCheckpoint.opening(root) as source_db:
            witness = ContextPrefix.read_prefix(source_db)
        with ExitStack() as lifetime:
            stream = lifetime.enter_context(source.open("rb"))
            revision = FileRevision.from_file(stream)
            witness.require_source(marker, stream)
            stage = Path(
                lifetime.enter_context(
                    tempfile.TemporaryDirectory(prefix=".move-context-", dir=root)
                )
            )
            # Failed preparation must not leave a partial destination store.
            # Carry's installer backup owns recovery once publication starts.
            store = ContextStore(stage)
            output_path = stage / BusHistory.filename
            with output_path.open("w+b") as output:
                os.chmod(output_path, 0o600)
                move = ContextMove(store)
                for line in witness.rows(stream):
                    line.move(move, output)
                if (
                    FileRevision.from_file(stream) != revision
                    or FileRevision.capture(source) != revision
                ):
                    raise RelationViolationError(
                        "Historical context source changed during conversion"
                    )
                output.flush()
                os.fsync(output.fileno())
                size = output.tell()
            derived = cls.derived_files(root)
            for path in derived:
                path.unlink()
            for path in ContextCheckpoint.files(index_path):
                path.unlink(missing_ok=True)
            os.replace(output_path, source)
            os.replace(store.path, ContextStore.path_in(root))
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return Carried(
                move.count + 1,
                {"bus_bytes": size, "requests": move.count},
                (
                    BusHistory.filename,
                    *(path.name for path in ContextCheckpoint.files(index_path)),
                    store.filename,
                    *(path.name for path in derived),
                ),
            )
