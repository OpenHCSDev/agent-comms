"""Move historical context rows once; retain every message and sealed claim row."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from abc import abstractmethod
from contextlib import ExitStack, closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar

from agent_comms.checkpoint_seals import FinalSeal
from agent_comms.context_store import (
    AbsentContext,
    ContextEntry,
    ContextStore,
    ContextStoreInfo,
    RecordedContextState,
)
from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.private_bus_checkpoint import (
    _SEED,
    CheckedPrefix,
    CheckpointTable,
    DeliverySources,
    WritableCheckpoint,
    _chain,
    _connect,
    _directory_sync,
    _tail,
)
from agent_comms.private_path import FileRevision
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.turn_context import (
    ContextEntryManifest,
    ContextEntryReference,
    ContextManifest,
    RecordedContextManifest,
    SegmentManifest,
)
from agent_comms.typed_table import Column, Index, SQLiteSchemaObject, TypedTable
from agent_comms.wire_log import WireLog
from agent_comms.wire_record import WireRecord, WireScan

from .carry import Carried, Carry


# These two old declarations exist only at this one-time format boundary.
@dataclass(frozen=True)
class ContextEntrySources(TypedTable):
    offset: int = field(metadata={"sql": Column(check="offset>=0")})
    length: int = field(metadata={"sql": Column(check="length>0")})
    session_id: str = field(metadata={"sql": Column(primary_key=True)})
    session_file: str = field(metadata={"sql": Column(primary_key=True)})
    entry_id: str = field(metadata={"sql": Column(primary_key=True)})


@dataclass(frozen=True)
class RecordedContextManifestSources(TypedTable):
    offset: int = field(metadata={"sql": Column(check="offset>=0")})
    length: int = field(metadata={"sql": Column(check="length>0")})
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

    def __init__(self, context_db: sqlite3.Connection, store: ContextStore) -> None:
        self.context_db, self.store = context_db, store
        self.source = ContextStoreInfo.source_in(context_db).initialized(context_db).source
        self.entries: dict[ContextEntryReference, SegmentManifest] = {}
        self.requests: dict[int, RecordedContextManifest] = {}
        self.values: dict[int, tuple[tuple[SegmentManifest, tuple[ContextEntryReference, ...]], ...]] = {}
        self.priors: dict[ThreadIncarnation, RecordedContextState] = {}
        self.count = 0

    def entry(self, entry: ContextEntryManifest) -> None:
        # The first value of a journal identity wins, as insert_if_absent keeps it.
        if entry.reference not in self.entries:
            self.entries[entry.reference] = entry.manifest
            ContextEntry.capture(entry).insert_if_absent(self.context_db)

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
            self.context_db,
            self.source,
            self.priors.get(record.thread, AbsentContext()),
            ContextManifest(record.thread, record.turn, segments, record.counter, record.request_id),
        )
        self.count += 1


class ContextManifests(Carry):
    staging_reserve_bytes: ClassVar[int] = 1024**3

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return (
            root / "bus.jsonl",
            root / "bus_meta.json",
            root / "private_bus_checkpoint.sqlite3",
            ContextStore.path_in(root),
            *cls.derived_files(root),
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

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = root / "private_bus_checkpoint.sqlite3"
        if not path.exists():
            return Carried(0)
        with closing(_connect(path, readonly=True)) as db:
            if not RecordedContextManifestSources.present(db):
                return Carried(0)
            count = RecordedContextManifestSources.count(db)
        if not apply:
            return Carried(count + 1)
        return cls._rewrite(root)

    @classmethod
    def _rewrite(cls, root: Path) -> Carried:
        """Convert the selected stopped root under Carry's existing wire lock.

        Practice runs select an already copied root through the normal Carry
        entrypoint. There is deliberately no live-source copying operation.
        """
        log = WireLog(root / "bus.jsonl")
        index_path = WritableCheckpoint.path_in(log.path.parent)
        required = log.path.stat().st_size + index_path.stat().st_size + cls.staging_reserve_bytes
        filesystem = os.statvfs(root)
        available = filesystem.f_bavail * filesystem.f_frsize
        if required > available:
            raise OSError(
                f"Context conversion requires {required} bytes; "
                f"available {available} bytes on {root}"
            )
        old_tables: tuple[type[TypedTable], ...] = (
            ContextEntrySources,
            RecordedContextManifestSources,
        )
        with ExitStack() as lifetime:
            with closing(_connect(index_path, readonly=True)) as source_db:
                marker = log.private_marker_unlocked()
                witness = CheckedPrefix.read_witness(source_db)
                marker.seal.check_final(witness, index_path)
                stream = lifetime.enter_context(log.path.open("rb"))
                witness.require_source(marker, os.fstat(stream.fileno()), stream)
                witness.require_current_revision(os.fstat(stream.fileno()))
                expected = {
                    name: sql
                    for table in (*TypedTable.members_with(CheckpointTable), *old_tables)
                    for name, sql in table.schema_objects().items()
                }
                if SQLiteSchemaObject.differences(
                    SQLiteSchemaObject.read(
                        source_db.execute(
                            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"
                        )
                    ),
                    expected,
                ):
                    raise RelationViolationError("Context conversion input SQL schema differs")
                stage = Path(
                    lifetime.enter_context(
                        tempfile.TemporaryDirectory(prefix=".move-context-", dir=root)
                    )
                )
                staged_index = stage / index_path.name
                staged_index.touch(mode=0o600)
                index_db = lifetime.enter_context(closing(sqlite3.connect(staged_index)))
                source_db.backup(index_db)
                index_db.row_factory = sqlite3.Row
            # The SQL reader is closed before processing any context records.
            # Carry still owns the stopped root's wire lock.
            if ContextStore.path_in(root).exists():
                raise RelationViolationError("Context conversion needs an absent destination store")
            # Failed preparation must not leave a partial destination store.
            # Carry's installer backup owns recovery once publication starts.
            store = ContextStore(stage)
            output_path = stage / "bus.jsonl"
            old_digest = new_digest = _SEED
            scan = WireScan(marker)
            with store.writing() as context_db, output_path.open("w+b") as output, index_db:
                os.chmod(output_path, 0o600)
                move = ContextMove(context_db, store)
                stream.seek(0)
                for offset, raw in WireRecord.rows(stream, end=witness.offset):
                    old_digest = _chain(old_digest, raw)
                    value = FieldCodec.decode(dict[str, object], FieldCodec.parse(raw))
                    if FieldCodec.record_key(ContextEnvelope, "observation") in value:
                        FieldCodec.decode(ContextEnvelope, value).observation.move(move, offset)
                    else:
                        # MessageKind owns historical info claims on current main.
                        # Keep every original message byte, including its kind label.
                        record = scan.read(raw)
                        for delivery in record.deliveries():
                            row = DeliverySources.require(index_db, seq=delivery.message.seq)
                            if (row.offset, row.length) != (offset, len(raw)):
                                raise RelationViolationError("Original message SQL pointer differs")
                            DeliverySources.update(
                                index_db,
                                where="seq=?",
                                parameters=(row.seq,),
                                offset=output.tell(),
                            )
                        output.write(raw)
                        new_digest = _chain(new_digest, raw)
                if (
                    old_digest.hex() != witness.digest
                    or scan.previous_sequence != witness.through_seq
                ):
                    raise RelationViolationError(
                        "Context conversion did not preserve the complete proved prefix"
                    )
                output.flush()
                os.fsync(output.fileno())
                size = output.tell()
                saved = CheckedPrefix.capture(
                    marker.root_id,
                    FileRevision.from_stat(os.fstat(output.fileno())),
                    witness.through_seq,
                    new_digest,
                    _tail(output, size),
                )
                for table in old_tables:
                    table.drop(index_db)
                CheckedPrefix.delete(index_db, where="singleton=1")
                saved.insert(index_db)
            index_db.close()
            derived = cls.derived_files(root)
            for path in derived:
                path.unlink()
            os.replace(output_path, log.path)
            # Rename changes ctime. Bind the canonical file's actual revision,
            # not the temporary name's earlier stat values.
            saved = replace(
                saved,
                mtime_ns=log.path.stat().st_mtime_ns,
                ctime_ns=log.path.stat().st_ctime_ns,
            )
            with closing(sqlite3.connect(staged_index)) as final_index, final_index:
                CheckedPrefix.delete(final_index, where="singleton=1")
                saved.insert(final_index)
            os.replace(staged_index, index_path)
            os.replace(store.path, ContextStore.path_in(root))
            _directory_sync(log.path)
            marker.seal_with(FinalSeal.capture(saved.witness(), index_path))
            log.write_metadata_unlocked(marker)
            return Carried(
                move.count + 1,
                {"bus_bytes": size, "requests": move.count},
                (
                    "bus.jsonl",
                    "bus_meta.json",
                    "private_bus_checkpoint.sqlite3",
                    store.filename,
                    *(path.name for path in derived),
                ),
            )


if __name__ == "__main__":
    ContextManifests.main()
