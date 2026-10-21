"""Move historical context rows once; retain every message and sealed claim row."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from abc import abstractmethod
from contextlib import ExitStack, closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, ClassVar

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
    WritableCheckpoint,
    _chain,
    _connect,
    _directory_sync,
)
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.turn_context import (
    ContextEntryManifest,
    ContextEntryReference,
    ContextManifest,
    RecordedContextManifest,
    SegmentManifest,
)
from agent_comms.typed_table import Column, Index, TypedTable
from agent_comms.wire_log import WireLog
from agent_comms.wire_record import WireRecord

from agent_comms.installer.carry import Carried, Carry


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

    def backup_files(self) -> tuple[Path, ...]:
        root = self.root
        return (
            root / "bus.jsonl",
            root / "bus_meta.json",
            root / "private_bus_checkpoint.sqlite3",
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
        path = root / "private_bus_checkpoint.sqlite3"
        if not path.exists():
            return Carried(0)
        with closing(_connect(path, readonly=True)) as db:
            if not RecordedContextManifestSources.present(db):
                return Carried(0)
            count = RecordedContextManifestSources.count(db)
        if not self.apply:
            return Carried(count + 1)
        return self._rewrite(root)

    @classmethod
    def _rewrite(cls, root: Path) -> Carried:
        """Convert the selected stopped root under Carry's existing wire lock.

        The old index proves the bus bytes before they move; it is never copied.
        The rewritten bus gets a fresh index derived from its own rows, so no old
        table, offset or seal survives the conversion. Practice runs select an
        already copied root through the normal Carry entrypoint.
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
        if ContextStore.path_in(root).exists():
            raise RelationViolationError("Context conversion needs an absent destination store")
        marker = log.private_marker_unlocked()
        with closing(_connect(index_path, readonly=True)) as source_db:
            witness = CheckedPrefix.read_witness(source_db)
        with ExitStack() as lifetime:
            stream = lifetime.enter_context(log.path.open("rb"))
            witness.require_source(marker, os.fstat(stream.fileno()), stream)
            stage = Path(
                lifetime.enter_context(
                    tempfile.TemporaryDirectory(prefix=".move-context-", dir=root)
                )
            )
            # Failed preparation must not leave a partial destination store.
            # Carry's installer backup owns recovery once publication starts.
            store = ContextStore(stage)
            output_path = stage / "bus.jsonl"
            digest = _SEED
            with store.writing() as context_db, output_path.open("w+b") as output:
                os.chmod(output_path, 0o600)
                move = ContextMove(context_db, store)
                stream.seek(0)
                for offset, raw in WireRecord.rows(stream, end=witness.offset):
                    digest = _chain(digest, raw)
                    value = FieldCodec.decode(dict[str, object], FieldCodec.parse(raw))
                    if FieldCodec.record_key(ContextEnvelope, "observation") in value:
                        FieldCodec.decode(ContextEnvelope, value).observation.move(move, offset)
                    else:
                        # Keep every original message byte, including its kind label.
                        output.write(raw)
                if digest.hex() != witness.digest:
                    raise RelationViolationError(
                        "Context conversion did not preserve the complete proved prefix"
                    )
                output.flush()
                os.fsync(output.fileno())
                size = output.tell()
            derived = cls.derived_files(root)
            for path in derived:
                path.unlink()
            index_path.unlink()
            os.replace(output_path, log.path)
            os.replace(store.path, ContextStore.path_in(root))
            _directory_sync(log.path)
            CheckedPrefix._install(log, marker)
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
