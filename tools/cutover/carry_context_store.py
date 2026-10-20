"""Move historical context rows once; retain every message and sealed claim row."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from abc import abstractmethod
from contextlib import ExitStack, closing
from dataclasses import dataclass, field, replace
from pathlib import Path

from agent_comms.checkpoint_seals import FinalSeal
from agent_comms.context_store import ContextEntry, ContextStore
from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import ClaimMessage, ClaimNoticeKind, InfoKind
from agent_comms.private_bus_checkpoint import (
    _SEED,
    CheckedPrefix,
    CheckpointTable,
    DeliverySources,
    _chain,
    _connect,
    _directory_sync,
    _path,
    _tail,
)
from agent_comms.private_path import FileRevision
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.turn_context import (
    ContextEntryManifest,
    RecordedContextManifest,
    RecordedSegmentManifest,
    TranscriptSegment,
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
    def move(self, move, offset): ...

    def recorded(self):
        raise RelationViolationError("Context predecessor is not a recorded request")


@dataclass(frozen=True)
class ContextEntryManifestObservation(ContextObservation):
    entries: tuple[ContextEntryManifest, ...]

    def move(self, move, offset):
        for entry in self.entries:
            move.entry(entry)


@dataclass(frozen=True)
class RecordedContextManifestObservation(ContextObservation):
    manifest: RecordedContextManifest

    def recorded(self):
        return self.manifest

    def move(self, move, offset):
        move.request(self.manifest, offset)


@dataclass(frozen=True)
class ContextEnvelope:
    observation: ContextObservation
    strict_fields = True


@dataclass
class MovedRequest:
    record_id: int
    recorded: RecordedContextManifest
    values: tuple
    references: tuple
    ordinal: int


class ContextMove:
    """One bounded look-behind per owner; original predecessor reads stay on the input."""

    def __init__(self, source, context_db, store):
        self.source, self.context_db, self.store = source, context_db, store
        self.previous = {}
        self.entries = set()
        self.count = 0

    def entry(self, entry):
        ContextEntry.capture(entry).insert_if_absent(self.context_db)
        self.entries.add(entry.reference)

    def original(self, offset):
        here = self.source.tell()
        try:
            self.source.seek(offset)
            raw = self.source.readline(WireRecord.max_row_bytes + 1)
            WireRecord.require_row_size(raw)
            return FieldCodec.decode(ContextEnvelope, FieldCodec.parse(raw)).observation.recorded()
        finally:
            self.source.seek(here)

    def value(self, record, position, offset):
        part = record.segments[position]
        header, references = part.manifest, part.entries
        for predecessor in part.predecessors(offset):
            prior = self.original(predecessor)
            if prior.thread != record.thread:
                raise RelationViolationError("Context predecessor belongs to another owner")
            old, old_references = self.value(prior, position, predecessor)
            if part.inherited_entries > len(old_references):
                raise RelationViolationError("Recorded context inherited entries are incomplete")
            references = (*old_references[: part.inherited_entries], *references)
            if not header.captured_text and old.sha256 == header.sha256:
                header = replace(header, captured_text=old.captured_text)
        return header, tuple(references)

    def request(self, record, offset):
        old = self.previous.get(record.thread)
        ordinal = 1 if old is None else old.ordinal + 1
        parts, values, all_references = [], [], []
        for position, _part in enumerate(record.segments):
            header, references = self.value(record, position, offset)
            compiled, entries = header.kind.record_manifest(header)
            if entries:
                for entry in entries:
                    self.entry(entry)
                references = compiled.entries
                header = replace(header, contributors=())
            if any(reference not in self.entries for reference in references):
                raise RelationViolationError("Original context request has missing entry records")
            previous = None
            if old is not None and position < len(old.recorded.segments):
                candidate = old.recorded.segments[position]
                if candidate.manifest.kind is header.kind:
                    previous = candidate
            if references:
                prior, keyframe = (), None
                if previous is not None and ordinal % TranscriptSegment.keyframe_interval:
                    prior, keyframe = previous.reference_keyframe(
                        old.references[position], old.record_id
                    )
                rewritten = RecordedSegmentManifest.entry_delta(header, references, prior, keyframe)
            elif previous is not None:
                rewritten = previous.record_text(header, old.values[position], old.record_id)
            else:
                rewritten = RecordedSegmentManifest(header)
            parts.append(rewritten)
            values.append(header)
            all_references.append(references)
        rewritten = replace(record, segments=tuple(parts))
        # The store owns logical IDs; legacy byte positions never become keys.
        record_id = self.store.append(self.context_db, rewritten)
        self.previous[record.thread] = MovedRequest(
            record_id, rewritten, tuple(values), tuple(all_references), ordinal
        )
        self.count += 1


class ContextManifests(Carry):
    @classmethod
    def backup_files(cls, root):
        return (
            root / "bus.jsonl",
            root / "bus_meta.json",
            root / "private_bus_checkpoint.sqlite3",
            ContextStore.path_in(root),
            *cls.derived_files(root),
        )

    @classmethod
    def derived_files(cls, root):
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
    def carry(cls, root, *, apply):
        path = root / "private_bus_checkpoint.sqlite3"
        if not path.exists():
            return Carried(0)
        with closing(_connect(path, readonly=True)) as db:
            old = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?",
                (RecordedContextManifestSources.declared_name,),
            ).fetchone()
            if old is None:
                return Carried(0)
            count = RecordedContextManifestSources.count(db)
        if not apply:
            return Carried(count + 1)
        return cls._rewrite(root)

    @staticmethod
    def message_bytes(value, raw):
        # Old claim envelopes were labeled info. Their existing claim object
        # binds the same seq/id/owner, but today's declaration is ClaimNoticeKind.
        # Repair only that label, never a claim, body, identity or signed row.
        kind_key = FieldCodec.record_key(ClaimMessage, "type")
        claim_key = FieldCodec.record_key(ClaimMessage, "claim_transition")
        if value.get(kind_key) != InfoKind.declared_name or claim_key not in value:
            return raw, 0
        from agent_comms.bus_publication import has_private_wire_fields

        if has_private_wire_fields(value):
            raise RelationViolationError("Unexpected signed legacy info claim")
        original = FieldCodec.decode(
            ClaimMessage, {key: item for key, item in value.items() if key != "id"}
        )
        changed = replace(original, type=ClaimNoticeKind).to_wire()
        if {key: item for key, item in value.items() if key != kind_key} != {
            key: item for key, item in changed.items() if key != kind_key
        }:
            raise RelationViolationError("Legacy claim conversion changed more than its kind label")
        return (json.dumps(changed, ensure_ascii=False, separators=(",", ":")) + "\n").encode(), 1

    @classmethod
    def _rewrite(cls, root):
        """Convert the selected stopped root under Carry's existing wire lock.

        Practice runs select an already copied root through the normal Carry
        entrypoint. There is deliberately no live-source copying operation.
        """
        source_log = target_log = WireLog(root / "bus.jsonl")
        index_path = _path(source_log.path)
        old_tables = (ContextEntrySources, RecordedContextManifestSources)
        with ExitStack() as lifetime:
            with closing(_connect(index_path, readonly=True)) as source_db:
                marker = source_log.private_marker_unlocked()
                witness = CheckedPrefix.read_witness(source_db)
                marker.seal.check_final(witness, index_path)
                stream = lifetime.enter_context(source_log.path.open("rb"))
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
                stage = lifetime.enter_context(
                    tempfile.TemporaryDirectory(prefix=".move-context-", dir=root)
                )
                stage = Path(stage)
                staged_index = stage / index_path.name
                staged_index.touch(mode=0o600)
                index_db = lifetime.enter_context(closing(sqlite3.connect(staged_index)))
                source_db.backup(index_db)
                index_db.row_factory = sqlite3.Row
            # The SQL reader is closed before processing any context records.
            # Carry still owns the stopped root's wire lock.
            if ContextStore.path_in(root).exists():
                raise RelationViolationError("Context conversion needs an absent destination store")
            store = ContextStore(root)
            output_path = stage / "bus.jsonl"
            old_digest = new_digest = _SEED
            scan = WireScan(marker)
            claim_labels = 0
            with store.writing() as context_db, output_path.open("w+b") as output, index_db:
                os.chmod(output_path, 0o600)
                move = ContextMove(stream, context_db, store)
                stream.seek(0)
                for number, (offset, raw) in enumerate(
                    WireRecord.rows(stream, end=witness.offset), 1
                ):
                    old_digest = _chain(old_digest, raw)
                    value = FieldCodec.parse(raw)
                    if "observation" in value:
                        FieldCodec.decode(ContextEnvelope, value).observation.move(move, offset)
                    else:
                        original_length = len(raw)
                        raw, changed = cls.message_bytes(value, raw)
                        claim_labels += changed
                        record = scan.read(raw)
                        for delivery in record.deliveries():
                            row = DeliverySources.one(index_db, seq=delivery.message.seq)
                            if row is None or (row.offset, row.length) != (offset, original_length):
                                raise RelationViolationError("Original message SQL pointer differs")
                            DeliverySources.update(
                                index_db, where="seq=?", parameters=(row.seq,), offset=output.tell()
                            )
                        output.write(raw)
                        new_digest = _chain(new_digest, raw)
                    if number % 256 == 0 and shutil.disk_usage(root).free < 10 * 1024**3:
                        raise OSError(
                            "Context conversion stopped before exhausting the 10GiB floor"
                        )
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
            os.replace(output_path, target_log.path)
            # Rename changes ctime. Bind the canonical file's actual revision,
            # not the temporary name's earlier stat values.
            saved = replace(
                saved,
                mtime_ns=target_log.path.stat().st_mtime_ns,
                ctime_ns=target_log.path.stat().st_ctime_ns,
            )
            with closing(sqlite3.connect(staged_index)) as final_index, final_index:
                CheckedPrefix.delete(final_index, where="singleton=1")
                saved.insert(final_index)
            os.replace(staged_index, _path(target_log.path))
            _directory_sync(target_log.path)
            marker.seal_with(FinalSeal.capture(saved.witness(), _path(target_log.path)))
            target_log.write_metadata_unlocked(marker)
            return Carried(
                move.count + 1,
                {"bus_bytes": size, "requests": move.count, "claim_labels_fixed": claim_labels},
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
