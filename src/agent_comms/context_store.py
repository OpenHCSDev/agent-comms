"""Append-only original turn contexts, independent of message delivery bytes."""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, ClassVar, Self

from agent_comms.errors import RelationViolationError, StoredHistoryUnreadable
from agent_comms.execution.context_entry_reader import ReadsContextEntries
from agent_comms.execution.context_prior import (
    ContextPrior,
    ContextStoreIdentity,
    ReadsContextPrior,
    RecordedContextState,
)
from agent_comms.execution.native_session_reopen import NativeSessionIdentity, PiSessionId
from agent_comms.execution.turn_context import (
    ContextEntryManifest,
    ContextEntryReference,
    ContextManifest,
    PreviouslyRecordedSegment,
    RecordedContextIdentity,
    RecordedContextManifest,
    RecordedSegmentManifest,
    SegmentManifest,
    SegmentRecording,
    UnrecordedSegment,
)
from agent_comms.execution.turn_item import NativeItemId
from agent_comms.nominal.field_codec import (
    ContextStoreId,
    FieldCodec,
    IdentityExclude,
    MalformedRecord,
    NullMember,
    TaggedRecordMember,
    TaggedUnion,
)
from agent_comms.registry.registration import Registration
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.storage.locked_store import InputCaptureFile
from agent_comms.storage.typed_table import (
    AttachedPracticeRebuild,
    Column,
    DurableStore,
    Generated,
    Index,
    RowidSequenced,
    RowNotFoundError,
    SingletonRow,
    StoreRows,
    TableSchema,
    TypedTable,
)


class ContextSourceState(ABC):
    @abstractmethod
    def initialized(self, db: sqlite3.Connection) -> ContextStoreInfo: ...

    @abstractmethod
    def latest(
        self, store: ContextStore, db: sqlite3.Connection, owner: ThreadIncarnation
    ) -> RecordedContextState: ...

    @abstractmethod
    def page(
        self,
        store: ContextStore,
        db: sqlite3.Connection,
        incarnation: ThreadIncarnation,
        cursor: ContextManifestCursor,
        previous: tuple[CapturedContext, ...],
        limit: int,
    ) -> ContextManifestPage: ...


class UnrecordedContextSource(ContextSourceState):
    def initialized(self, db: sqlite3.Connection) -> ContextStoreInfo:
        stored = ContextStoreInfo(1, ContextStoreIdentity(ContextStoreId.mint()))
        stored.insert(db)
        return stored

    def latest(
        self, store: ContextStore, db: sqlite3.Connection, owner: ThreadIncarnation
    ) -> RecordedContextState:
        return AbsentContext()

    def page(
        self,
        store: ContextStore,
        db: sqlite3.Connection,
        incarnation: ThreadIncarnation,
        cursor: ContextManifestCursor,
        previous: tuple[CapturedContext, ...],
        limit: int,
    ) -> ContextManifestPage:
        return ContextManifestPage(())


class ContextStoreRows(StoreRows):
    """The tables of ContextStore; each row declares itself one."""


@dataclass(frozen=True)
class ContextStoreInfo(ContextStoreRows, ContextSourceState, SingletonRow):
    source: ContextStoreIdentity

    @classmethod
    def source_in(cls, db: sqlite3.Connection) -> ContextSourceState:
        rows = cls.select(db, singleton=1)
        if rows:
            return rows[0]
        if ContextRequest.exists(db):
            raise RelationViolationError("Recorded context store identity is missing")
        return UnrecordedContextSource()

    def initialized(self, db: sqlite3.Connection) -> ContextStoreInfo:
        return self

    def latest(
        self, store: ContextStore, db: sqlite3.Connection, owner: ThreadIncarnation
    ) -> RecordedContextState:
        return store.latest(db, self.source, owner)

    def page(
        self,
        store: ContextStore,
        db: sqlite3.Connection,
        incarnation: ThreadIncarnation,
        cursor: ContextManifestCursor,
        previous: tuple[CapturedContext, ...],
        limit: int,
    ) -> ContextManifestPage:
        where, parameters = cursor.selection(self.source, incarnation)
        candidates = ContextRequest.select(
            db,
            where=where,
            parameters=parameters,
            order_by=("id",),
            descending=cursor.descending,
            limit=limit,
        )
        rows: list[ContextRequest] = []
        size = 0
        for row in candidates:
            if rows and size + row.size > store.max_page_bytes:
                break
            rows.append(row)
            size += row.size
        rows.sort(key=lambda row: row.id)
        older: BeforeContextManifest | LatestContextManifest = LATEST_CONTEXT
        newer: AfterContextManifest | LatestContextManifest = LATEST_CONTEXT
        if rows:
            older = BeforeContextManifest(self.source, rows[0].id).available(db, incarnation)
            newer = AfterContextManifest(self.source, rows[-1].id).available(db, incarnation)
        retained = {item.record_id: item for item in previous if item.source == self.source}
        resolved = {item.record_id: item for item in store.resolve(
            db, self.source, (row for row in rows if row.id not in retained),
        )}
        resources = tuple(
            retained[row.id] if row.id in retained else resolved[row.id] for row in rows
        )
        return ContextManifestPage(resources, older, newer)


@dataclass(frozen=True)
class ContextEntry(ContextStoreRows, TypedTable):
    session_id: Annotated[PiSessionId, Column(primary_key=True)]
    session_file: Annotated[str, Column(primary_key=True)]
    entry_id: Annotated[NativeItemId, Column(primary_key=True)]
    manifest: SegmentManifest

    @classmethod
    def capture(cls, entry: ContextEntryManifest) -> Self:
        ref = entry.reference
        return cls(ref.identity.session_id, ref.identity.session_file, ref.entry_id, entry.manifest)

    @classmethod
    def originals(
        cls, db: sqlite3.Connection, references: Collection[ContextEntryReference],
    ) -> dict[ContextEntryReference, SegmentManifest]:
        """Every referenced entry's first manifest from one keyed read; a missing row is
        corruption. One select per entry was 902 statements for one channel message."""
        stored = {(row.session_id, row.session_file, row.entry_id): row.manifest
                  for row in cls.select_keys(db, tuple(
                      {"session_id": reference.identity.session_id,
                       "session_file": reference.identity.session_file,
                       "entry_id": reference.entry_id}
                      for reference in references))}
        manifests = {}
        for reference in references:
            key = (reference.identity.session_id, reference.identity.session_file,
                   reference.entry_id)
            if key not in stored:
                raise RowNotFoundError(f"Expected one {cls.declared_name} for {key}")
            manifests[reference] = stored[key]
        return manifests


@dataclass(frozen=True)
class ContextSegmentVersion(ContextStoreRows, RowidSequenced):
    id: Annotated[int, Column(primary_key=True, auto_increment=True)]
    digest: str
    segment: RecordedSegmentManifest
    indexes = (Index(("digest",), unique=True),)

    @classmethod
    def append(cls, db: sqlite3.Connection, segment: RecordedSegmentManifest) -> int:
        digest = FieldCodec.digest(segment)
        rows = cls.select(db, digest=digest)
        if rows:
            existing = rows[0]
            if existing.segment != segment:
                raise RelationViolationError("Context segment version hash collision")
            return existing.id
        return cls.insert_sequence(db, digest=digest, segment=segment)


@dataclass(frozen=True)
class ContextRequest(ContextStoreRows, RowidSequenced):
    id: Annotated[int, Column(primary_key=True, auto_increment=True)]
    identity: RecordedContextIdentity
    thread_birth: ClassVar[Generated] = Generated(
        float,
        Column(generated="json_extract(identity,'$.thread.created_at')"),
    )
    """The recorded incarnation's identity; its name is only a route."""
    versions: tuple[int, ...]
    size: int
    indexes = (Index(("thread_birth", "id")),)

    @classmethod
    def owner_filter(cls, owner: ThreadIncarnation) -> tuple[str, tuple[object, ...]]:
        return "thread_birth=?", (owner.require_recorded().created_at,)

    @classmethod
    def newest(cls, db: sqlite3.Connection, owner: ThreadIncarnation) -> list[ContextRequest]:
        """The thread's newest request, read through the owner index."""
        return cls.select(
            db,
            thread_birth=owner.require_recorded().created_at,
            order_by=("id",),
            descending=True,
            limit=1,
        )

    @classmethod
    def append(cls, db: sqlite3.Connection, record: RecordedContextManifest) -> int:
        versions = tuple(ContextSegmentVersion.append(db, part) for part in record.segments)
        identity = FieldCodec.decode(
            RecordedContextIdentity, FieldCodec.project(record, IdentityExclude)
        )
        identity.thread.require_recorded()
        return cls.insert_sequence(
            db,
            identity=identity,
            versions=versions,
            size=len(json.dumps(FieldCodec.encode(record)).encode()),
        )

    def recorded(self, db: sqlite3.Connection) -> RecordedContextManifest:
        parts = []
        for version in self.versions:
            parts.append(ContextSegmentVersion.require(db, id=version).segment)
        return self.identity.with_segments(tuple(parts))

    @classmethod
    def retain_required_rows(
        cls, original: AttachedPracticeRebuild, schema: type[TableSchema],
    ) -> int:
        """The stored request owns its serialized version, entry and predecessor edges."""
        changed = super().retain_required_rows(original, schema)
        db = original.destination
        for request in cls.select(db):
            changed += original.retain_rows(ContextStoreInfo)
            ContextStoreInfo.require(db, singleton=1)
            changed += original.retain_keys(
                ContextSegmentVersion, tuple({"id": version} for version in request.versions),
            )
            record = request.recorded(db)  # Missing original versions refuse, never disappear.
            for part in record.segments:
                predecessors = part.predecessors(request.id)
                changed += original.retain_keys(cls, tuple({"id": key} for key in predecessors))
                for key in predecessors:
                    if cls.require(db, id=key).identity.thread != request.identity.thread:
                        raise RelationViolationError("Context predecessor is missing or foreign")
                changed += original.retain_keys(ContextEntry, tuple(
                    {"session_id": ref.identity.session_id,
                     "session_file": ref.identity.session_file, "entry_id": ref.entry_id}
                    for ref in part.entries
                ))
                ContextEntry.originals(db, part.entries)
        return changed



class AbsentContext(RecordedContextState):
    def next_recording_turn(self) -> int:
        return 1

    def require_newest(
        self, db: sqlite3.Connection, source: ContextStoreIdentity, owner: ThreadIncarnation
    ) -> None:
        if ContextRequest.newest(db, owner):
            raise RelationViolationError(
                f"{owner.name} has recorded context this writer never read"
            )

    def segment(self, position: int, original: SegmentManifest) -> SegmentRecording:
        return UnrecordedSegment()


@dataclass(frozen=True)
class CapturedContext(RecordedContextState):
    source: ContextStoreIdentity
    record_id: int
    recorded: RecordedContextManifest
    value: ContextManifest

    def next_recording_turn(self) -> int:
        return self.recorded.recording_turn + 1

    def require_newest(
        self, db: sqlite3.Connection, source: ContextStoreIdentity, owner: ThreadIncarnation
    ) -> None:
        newest = ContextRequest.newest(db, owner)
        if source != self.source or not newest or newest[0].id != self.record_id:
            raise RelationViolationError(
                f"{owner.name} recorded context after request {self.record_id}"
            )

    def segment(self, position: int, original: SegmentManifest) -> SegmentRecording:
        if position < len(self.value.segments):
            previous = self.value.segments[position]
            if previous.kind is original.kind:
                return PreviouslyRecordedSegment(
                    self.recorded.segments[position], previous, self.record_id
                )
        return UnrecordedSegment()


class ContextManifestCursor(TaggedUnion, affix="ContextManifest"):
    """Where a context page starts: the latest manifests, or before or after one. A remote UI
    sends it to Core, so a positioned cursor carries its direction as its kind."""

    descending: ClassVar[bool] = True


    @abstractmethod
    def has_position(self) -> bool: ...

    @abstractmethod
    def selection(
        self, source: ContextStoreIdentity, owner: ThreadIncarnation
    ) -> tuple[str, tuple[object, ...]]: ...


@dataclass(frozen=True)
class LatestContextManifest(NullMember, ContextManifestCursor):
    def has_position(self) -> bool:
        return False

    def selection(
        self, source: ContextStoreIdentity, owner: ThreadIncarnation
    ) -> tuple[str, tuple[object, ...]]:
        return ContextRequest.owner_filter(owner)


@dataclass(frozen=True)
class PositionedContextManifest(ContextManifestCursor, declared_name=None):
    source: ContextStoreIdentity
    record_id: int
    comparison: ClassVar[str]
    descending: ClassVar[bool]

    def has_position(self) -> bool:
        return True

    def selection(
        self, source: ContextStoreIdentity, owner: ThreadIncarnation
    ) -> tuple[str, tuple[object, ...]]:
        if self.source != source:
            raise ValueError("Recorded context page changed its source")
        where, parameters = ContextRequest.owner_filter(owner)
        return where + f" AND id {self.comparison} ?", (*parameters, self.record_id)

    def available(
        self,
        db: sqlite3.Connection,
        owner: ThreadIncarnation,
    ) -> Self | LatestContextManifest:
        where, parameters = ContextRequest.owner_filter(owner)
        return (
            self
            if ContextRequest.exists(
                db,
                where=where + f" AND id {self.comparison} ?",
                parameters=(*parameters, self.record_id),
            )
            else LATEST_CONTEXT
        )


class BeforeContextManifest(TaggedRecordMember, PositionedContextManifest):
    comparison = "<"
    descending = True


class AfterContextManifest(TaggedRecordMember, PositionedContextManifest):
    comparison = ">"
    descending = False


LATEST_CONTEXT = LatestContextManifest()


@dataclass(frozen=True)
class ContextManifestReference:
    source: ContextStoreIdentity
    record_id: int

    def read(
        self,
        store: ContextStore,
        name: str,
        registry: Registration,
    ) -> ContextManifest:
        page = store.page(
            name, registry, cursor=BeforeContextManifest(self.source, self.record_id + 1), limit=1
        )
        if len(page.resources) != 1 or page.resources[0].record_id != self.record_id:
            raise MalformedRecord("Recorded context reference is absent from this owner")
        return page.resources[0].value


@dataclass(frozen=True)
class ContextManifestPage:
    resources: tuple[CapturedContext, ...]
    older: BeforeContextManifest | LatestContextManifest = field(
        default_factory=LatestContextManifest
    )
    newer: AfterContextManifest | LatestContextManifest = field(
        default_factory=LatestContextManifest
    )

    def has_older(self) -> bool:
        return self.older.has_position()

    def has_newer(self) -> bool:
        return self.newer.has_position()

    def reference(self, manifest: ContextManifest) -> ContextManifestReference:
        for resource in self.resources:
            if resource.value is manifest:
                return ContextManifestReference(resource.source, resource.record_id)
        raise ValueError("Recorded context is outside the selected page")


@dataclass(frozen=True)
class ContextStore(ReadsContextEntries, ReadsContextPrior, DurableStore, InputCaptureFile):
    root: Path
    filename: ClassVar[str] = "turn_context.sqlite3"
    tables = ContextStoreRows
    max_page_rows: ClassVar[int] = 8
    max_page_bytes: ClassVar[int] = 512 * 1024
    schema_error = StoredHistoryUnreadable

    @contextmanager
    def snapshot(self) -> Iterator[sqlite3.Connection]:
        try:
            with super().snapshot() as db:
                yield db
        except (MalformedRecord, sqlite3.DatabaseError) as error:
            raise StoredHistoryUnreadable(f"Unreadable recorded context: {error}") from error

    def latest(
        self,
        db: sqlite3.Connection,
        source: ContextStoreIdentity,
        owner: ThreadIncarnation,
    ) -> RecordedContextState:
        rows = ContextRequest.newest(db, owner)
        if not rows:
            return AbsentContext()
        return next(self.resolve(db, source, rows))

    def entries(
        self, identity: NativeSessionIdentity, entry_ids: Iterable[NativeItemId],
    ) -> dict[NativeItemId, SegmentManifest]:
        """The recorded manifest of each named journal entry that has one."""
        with self.snapshot() as db:
            return {row.entry_id: row.manifest for entry_id in entry_ids
                    for row in ContextEntry.select(db, session_id=identity.session_id,
                                                   session_file=identity.session_file,
                                                   entry_id=entry_id)}

    def original_segments(self) -> Iterator[SegmentManifest]:
        """Original segments actually present in this selected entry/request store.

        Requests restore through the same original predecessor/entry reader as
        the browser. Entry-only stores need no invented request/store identity.
        """
        with self.snapshot() as db:
            for entry in ContextEntry.stream(db):
                yield from entry.manifest.original_values()
            requests = tuple(ContextRequest.stream(db))
            if requests:
                source = ContextStoreInfo.require(db, singleton=1).source
                for request in self.resolve(db, source, requests):
                    for root in request.value.segments:
                        yield from root.original_values()

    def retain_contexts(
        self, destination: ContextStore, identities: Iterable[NativeSessionIdentity], *,
        maximum_bytes: int, requests: Iterable[ContextManifestReference] = (),
    ) -> None:
        """Retain original entry manifests and explicitly selected saved requests.

        Raw writable values, source identity, predecessor/version/entry closure
        and sequence high-water marks travel unchanged. Session paths are keys,
        not relocated storage or permission to launch or replay a native session.
        Entry-only selection retains no request or fabricated store identity.
        """
        with type(self).retaining_rows(
            self.path, destination.path, maximum_bytes=maximum_bytes,
        ) as original:
            original.retain_keys(ContextEntry, tuple(
                {"session_id": identity.session_id, "session_file": identity.session_file}
                for identity in dict.fromkeys(identities)
            ))
            selected = tuple(dict.fromkeys(requests))
            if selected:
                original.retain_rows(ContextStoreInfo)
                source = ContextStoreInfo.require(original.destination, singleton=1).source
                for reference in selected:
                    if reference.source != source:
                        raise RelationViolationError("Selected context belongs to another store")
                original.retain_keys(ContextRequest, tuple(
                    {"id": reference.record_id} for reference in selected
                ))
                for reference in selected:
                    ContextRequest.require(original.destination, id=reference.record_id)

    def require_retained(
        self, destination: ContextStore, identities: tuple[NativeSessionIdentity, ...],
        requests: tuple[ContextManifestReference, ...],
    ) -> None:
        """Compare selected originals through the browser's entry and request readers.

        The caller owns the cross-store capture lifetime. Resolve every retained
        request, including predecessors, rather than comparing only its final
        displayed value or recreating the serialized version/entry relationship.
        """
        with self.snapshot() as source, destination.snapshot() as retained:
            for identity in identities:
                source_entries, retained_entries = ({row.entry_id: row for row in ContextEntry.select(
                    db, session_id=identity.session_id, session_file=identity.session_file,
                )} for db in (source, retained))
                if source_entries != retained_entries:
                    raise RelationViolationError("Selected context entries differ from their originals")
            for reference in requests:
                stored = ContextStoreInfo.require(retained, singleton=1).source
                if stored != reference.source:
                    raise RelationViolationError("Retained context reference names another source")
                ContextRequest.require(retained, id=reference.record_id)
            rows = tuple(ContextRequest.stream(retained))
            if rows:
                original_source = ContextStoreInfo.require(source, singleton=1).source
                retained_source = ContextStoreInfo.require(retained, singleton=1).source
                originals = tuple(ContextRequest.require(source, id=row.id) for row in rows)
                if rows != originals:
                    raise RelationViolationError("Retained context reader differs from its original")
                expected = self.resolve(source, original_source, originals)
                actual = destination.resolve(retained, retained_source, rows)
                for original, copied in zip(expected, actual, strict=True):
                    if copied != original:
                        raise RelationViolationError("Retained context reader differs from its original")
            for version in ContextSegmentVersion.stream(retained):
                if version != ContextSegmentVersion.require(source, id=version.id):
                    raise RelationViolationError("Retained context version differs from its original")

    def prior(self, owner: ThreadIncarnation) -> RecordedContextState:
        """Read the thread's previous request from a snapshot, outside any write lock."""
        with self.snapshot() as db:
            return ContextStoreInfo.source_in(db).latest(self, db, owner)

    def record_after(self, prior: ContextPrior, manifest: ContextManifest) -> CapturedContext:
        """Append ``manifest`` after ``prior``; the result is the next request's prior,
        so a writer holding its last request never resolves it again (resolving decodes
        every entry of the thread's context: 16% of the worker's CPU per channel message)."""
        recorded = prior.read(self, manifest.thread)
        record, entries = recorded.capture(manifest)
        record.turn.require_recorded()
        with self.writing() as db:
            source = ContextStoreInfo.source_in(db).initialized(db).source
            recorded.require_newest(db, source, manifest.thread)
            for entry in entries:
                # A journal identity owns the first entry value across requests.
                ContextEntry.capture(entry).insert_if_absent(db)
            return CapturedContext(source, ContextRequest.append(db, record), record, manifest)

    def resolve(
        self,
        db: sqlite3.Connection,
        source: ContextStoreIdentity,
        rows: Iterable[ContextRequest],
    ) -> Iterator[CapturedContext]:
        """Resolve one snapshot's requested rows, decoding shared originals once.

        Only selected rows and their direct referenced segments participate; the
        maps die with this iterator and never cross a snapshot or retain history.
        """
        selected = tuple(rows)
        originals = {row.id: row for row in selected}
        records = {key: row.recorded(db) for key, row in originals.items()}
        references: set[ContextEntryReference] = set()
        for row in selected:
            for position, part in enumerate(records[row.id].segments):
                references.update(part.entries)
                for predecessor in part.predecessors(row.id):
                    if predecessor not in originals:
                        originals[predecessor] = ContextRequest.require(db, id=predecessor)
                    prior = originals[predecessor]
                    if prior.identity.thread != row.identity.thread:
                        raise RelationViolationError("Context predecessor is missing or foreign")
                    if predecessor not in records:
                        records[predecessor] = prior.recorded(db)
                    # Only the referenced segment is needed, not the predecessor
                    # request's other segments and their unrelated dependencies.
                    prior_part = part.predecessor(records, position)
                    references.update(prior_part.entries)
        entries = ContextEntry.originals(db, references)
        for row in selected:
            record = records[row.id]
            yield CapturedContext(source, row.id, record, record.restore(entries, records))

    def page(
        self,
        name: str,
        registry: Registration,
        *,
        cursor: ContextManifestCursor = LATEST_CONTEXT,
        previous: tuple[CapturedContext, ...] = (),
        limit: int = max_page_rows,
    ) -> ContextManifestPage:
        if not 1 <= limit <= self.max_page_rows:
            raise ValueError("Context page row limit exceeds its source budget")
        incarnation = registry.snapshot().require(name).incarnation
        selection = cursor
        with self.snapshot() as db:
            return ContextStoreInfo.source_in(db).page(
                self, db, incarnation, selection, previous, limit
            )
