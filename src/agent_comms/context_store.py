"""Append-only original turn contexts, independent of message delivery bytes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal, cast

from .errors import RelationViolationError, StoredHistoryUnreadable
from .field_codec import FieldCodec, Id128Text, MalformedRecord
from .turn_context import (
    ContextEntryManifest,
    ContextEntryReference,
    ContextManifest,
    RecordedContextIdentity,
    RecordedContextManifest,
    RecordedSegmentManifest,
    SegmentManifest,
)
from .typed_table import Column, Index, TypedStore, TypedTable


@dataclass(frozen=True)
class ContextStoreIdentity:
    value: str

    def __post_init__(self):
        Id128Text.from_text(self.value)


@dataclass(frozen=True)
class ContextStoreInfo(TypedTable):
    singleton: Literal[1] = field(metadata={"sql": Column(primary_key=True)})
    source: ContextStoreIdentity


@dataclass(frozen=True)
class ContextEntry(TypedTable):
    session_id: str = field(metadata={"sql": Column(primary_key=True)})
    session_file: str = field(metadata={"sql": Column(primary_key=True)})
    entry_id: str = field(metadata={"sql": Column(primary_key=True)})
    manifest: SegmentManifest

    @classmethod
    def capture(cls, entry: ContextEntryManifest):
        ref = entry.reference
        return cls(ref.identity.session_id, ref.identity.session_file, ref.entry_id, entry.manifest)

    @classmethod
    def original(cls, db, reference: ContextEntryReference) -> SegmentManifest:
        row = cls.one(
            db,
            session_id=reference.identity.session_id,
            session_file=reference.identity.session_file,
            entry_id=reference.entry_id,
        )
        if row is None:
            raise RelationViolationError("Recorded context entry is missing")
        return row.manifest


@dataclass(frozen=True)
class ContextSegmentVersion(TypedTable):
    id: int = field(metadata={"sql": Column(primary_key=True, auto_increment=True)})
    digest: str
    segment: RecordedSegmentManifest
    indexes = (Index(("digest",), unique=True),)

    @classmethod
    def append(cls, db, segment: RecordedSegmentManifest) -> int:
        encoded = json.dumps(FieldCodec.encode(segment), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        row = cls.one(db, digest=digest)
        if row is not None:
            if row.segment != segment:
                raise RelationViolationError("Context segment version hash collision")
            return row.id
        return cast(int, cls.insert_values(db, digest=digest, segment=segment).lastrowid)


@dataclass(frozen=True)
class ContextRequest(TypedTable):
    id: int = field(metadata={"sql": Column(primary_key=True, auto_increment=True)})
    identity: RecordedContextIdentity
    thread_name: str = field(
        metadata={"sql": Column(generated="json_extract(identity,'$.thread.name')")}
    )
    thread_birth: float = field(
        metadata={"sql": Column(generated="json_extract(identity,'$.thread.created_at')")}
    )
    versions: tuple[int, ...]
    size: int
    indexes = (Index(("thread_name", "thread_birth", "id")),)

    @classmethod
    def owner_filter(cls, owners):
        return (
            "(thread_name,thread_birth) IN (" + ",".join("(?,?)" for _ in owners) + ")",
            tuple(value for owner in owners for value in (owner.name, owner.wire_birth)),
        )

    @classmethod
    def append(cls, db, record: RecordedContextManifest) -> int:
        versions = tuple(ContextSegmentVersion.append(db, part) for part in record.segments)
        identity = FieldCodec.decode(
            RecordedContextIdentity, FieldCodec.project(record, "identity")
        )
        return cast(
            int,
            cls.insert_values(
                db,
                identity=identity,
                versions=versions,
                size=len(json.dumps(FieldCodec.encode(record)).encode()),
            ).lastrowid,
        )

    def recorded(self, db) -> RecordedContextManifest:
        parts = []
        for version in self.versions:
            row = ContextSegmentVersion.one(db, id=version)
            if row is None:
                raise RelationViolationError("Recorded context segment version is missing")
            parts.append(row.segment)
        return self.identity.with_segments(tuple(parts))


@dataclass(frozen=True)
class CapturedContext:
    source: ContextStoreIdentity
    record_id: int
    recorded: RecordedContextManifest
    value: ContextManifest


@dataclass(frozen=True)
class ContextManifestCursor:
    source: ContextStoreIdentity
    record_id: int
    comparison: ClassVar[str]
    descending: ClassVar[bool]

    def available(self, db, owners):
        where, parameters = ContextRequest.owner_filter(owners)
        return (
            self
            if ContextRequest.exists(
                db,
                where=where + f" AND id {self.comparison} ?",
                parameters=(*parameters, self.record_id),
            )
            else None
        )


class BeforeContextManifest(ContextManifestCursor):
    comparison = "<"
    descending = True


class AfterContextManifest(ContextManifestCursor):
    comparison = ">"
    descending = False


@dataclass(frozen=True)
class ContextManifestReference:
    source: ContextStoreIdentity
    record_id: int

    def read(self, store: ContextStore, name, registry) -> ContextManifest:
        page = store.page(
            name, registry, cursor=BeforeContextManifest(self.source, self.record_id + 1), limit=1
        )
        if len(page.resources) != 1 or page.resources[0].record_id != self.record_id:
            raise MalformedRecord("Recorded context reference is absent from this owner")
        return page.resources[0].value


@dataclass(frozen=True)
class ContextManifestPage:
    resources: tuple[CapturedContext, ...]
    older: BeforeContextManifest | None
    newer: AfterContextManifest | None

    def has_older(self) -> bool:
        return self.older is not None

    def has_newer(self) -> bool:
        return self.newer is not None

    def reference(self, manifest: ContextManifest) -> ContextManifestReference:
        for resource in self.resources:
            if resource.value is manifest:
                return ContextManifestReference(resource.source, resource.record_id)
        raise ValueError("Recorded context is outside the selected page")


@dataclass(frozen=True)
class ContextStore(TypedStore):
    root: Path
    filename: ClassVar[str] = "turn_context.sqlite3"
    rows = (ContextStoreInfo, ContextEntry, ContextSegmentVersion, ContextRequest)
    max_page_rows: ClassVar[int] = 8
    max_page_bytes: ClassVar[int] = 512 * 1024
    schema_error = StoredHistoryUnreadable

    @contextmanager
    def snapshot(self):
        try:
            with super().snapshot() as db:
                yield db
        except (MalformedRecord, sqlite3.DatabaseError) as error:
            raise StoredHistoryUnreadable(f"Unreadable recorded context: {error}") from error

    def initialize(self, db) -> ContextStoreIdentity:
        row = ContextStoreInfo.one(db, singleton=1)
        if row is None:
            row = ContextStoreInfo(1, ContextStoreIdentity(uuid.uuid4().hex))
            row.insert(db)
        return row.source

    def record(self, manifest: ContextManifest) -> None:
        with self.writing() as db:
            source = self.initialize(db)
            previous = ContextRequest.select(
                db,
                thread_name=manifest.thread.name,
                thread_birth=manifest.thread.wire_birth,
                order_by=("id",),
                descending=True,
                limit=1,
            )
            prior = self.resolve(db, source, previous[0]) if previous else None
            record, entries = RecordedContextManifest.capture(manifest, prior)
            self.append(db, record, entries)

    def append(self, db, record: RecordedContextManifest, entries=()) -> int:
        self.initialize(db)
        record.turn.require_recorded()
        for entry in entries:
            # The journal identity owns the entry, not each later request's
            # publication clock. Preserve the first recorded entry value.
            ContextEntry.capture(entry).insert_if_absent(db)
        return ContextRequest.append(db, record)

    def resolve(self, db, source: ContextStoreIdentity, row: ContextRequest) -> CapturedContext:
        records = {row.id: row.recorded(db)}
        references = set()
        for position, part in enumerate(records[row.id].segments):
            references.update(part.entries)
            for predecessor in part.predecessors(row.id):
                if predecessor not in records:
                    prior = ContextRequest.one(db, id=predecessor)
                    if prior is None or prior.identity.thread != row.identity.thread:
                        raise RelationViolationError("Context predecessor is missing or foreign")
                    records[predecessor] = prior.recorded(db)
                # Only the referenced segment is needed, not the predecessor
                # request's other segments and their unrelated dependencies.
                prior_part = part.predecessor(records, position)
                references.update(prior_part.entries)
        entries = {ref: ContextEntry.original(db, ref) for ref in references}
        record = records[row.id]
        return CapturedContext(source, row.id, record, record.restore(entries, records))

    def page(self, name, registry, *, cursor=None, previous=(), limit=None) -> ContextManifestPage:
        limit = self.max_page_rows if limit is None else limit
        if not 1 <= limit <= self.max_page_rows:
            raise ValueError("Context page row limit exceeds its source budget")
        snapshot = registry.snapshot()
        incarnation = snapshot.require(name).incarnation
        owners = incarnation.recorded_names(snapshot)
        with self.snapshot() as db:
            info = ContextStoreInfo.one(db, singleton=1)
            if info is None:
                if ContextRequest.exists(db):
                    raise RelationViolationError("Recorded context store identity is missing")
                return ContextManifestPage((), None, None)
            source = info.source
            where, parameters = ContextRequest.owner_filter(owners)
            if cursor is not None:
                if cursor.source != source:
                    raise ValueError("Recorded context page changed its source")
                where += f" AND id {cursor.comparison} ?"
                parameters += (cursor.record_id,)
            descending = cursor is None or cursor.descending
            candidates = ContextRequest.select(
                db,
                where=where,
                parameters=parameters,
                order_by=("id",),
                descending=descending,
                limit=limit,
            )
            rows, size = [], 0
            for row in candidates:
                if rows and size + row.size > self.max_page_bytes:
                    break
                rows.append(row)
                size += row.size
            rows.sort(key=lambda row: row.id)
            older = newer = None
            if rows:
                older = BeforeContextManifest(source, rows[0].id).available(db, owners)
                newer = AfterContextManifest(source, rows[-1].id).available(db, owners)
            retained = {item.record_id: item for item in previous if item.source == source}
            resources = tuple(
                retained[row.id] if row.id in retained else self.resolve(db, source, row)
                for row in rows
            )
            for resource in resources:
                if resource.value.thread.resolved(snapshot) != incarnation:
                    raise RelationViolationError("Context source differs from recorded owner")
            return ContextManifestPage(resources, older, newer)
