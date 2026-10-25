"""Append-only original turn contexts, independent of message delivery bytes."""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, ClassVar, Self

from .errors import RelationViolationError, StoredHistoryUnreadable
from .field_codec import ContextStoreId, FieldCodec, MalformedRecord, NullMember
from .native_session_reopen import NativeSessionIdentity
from .registration import Registration
from .thread_identity import ThreadIncarnation
from .turn_context import (
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

from .typed_table import StoreRows, Column, DurableStore, Generated, Index, RowidSequenced, SingletonRow, TypedTable
from .turn_item import NativeItemId
from .native_session_reopen import PiSessionId


@dataclass(frozen=True)
class ContextStoreIdentity:
    value: ContextStoreId


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
        resources = tuple(
            retained[row.id] if row.id in retained else store.resolve(db, self.source, row)
            for row in rows
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
    def original(
        cls,
        db: sqlite3.Connection,
        reference: ContextEntryReference,
    ) -> SegmentManifest:
        return cls.require(
            db,
            session_id=reference.identity.session_id,
            session_file=reference.identity.session_file,
            entry_id=reference.entry_id,
        ).manifest


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
            RecordedContextIdentity, FieldCodec.project(record, "identity")
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


class ContextPriorSupersededError(RelationViolationError):
    """Another writer recorded this thread's context after the prior this writer holds."""


class ContextPrior(ABC):
    """What a context writer knows about its thread's previous request."""

    @abstractmethod
    def read(self, store: ContextStore, owner: ThreadIncarnation) -> RecordedContextState: ...


class UnreadContextPrior(ContextPrior):
    """A writer that has not yet read its thread's previous request."""

    def read(self, store: ContextStore, owner: ThreadIncarnation) -> RecordedContextState:
        return store.prior(owner)


UNREAD_CONTEXT = UnreadContextPrior()


class RecordedContextState(ContextPrior):
    """The previous request, including the first request's lack of a predecessor."""

    def read(self, store: ContextStore, owner: ThreadIncarnation) -> RecordedContextState:
        return self

    @abstractmethod
    def require_newest(
        self, db: sqlite3.Connection, source: ContextStoreIdentity, owner: ThreadIncarnation
    ) -> None:
        """Refuse when another writer recorded this thread after this prior."""

    @abstractmethod
    def next_recording_turn(self) -> int: ...

    @abstractmethod
    def segment(self, position: int, original: SegmentManifest) -> SegmentRecording: ...

    def capture(
        self, manifest: ContextManifest
    ) -> tuple[RecordedContextManifest, tuple[ContextEntryManifest, ...]]:
        return RecordedContextManifest.capture(manifest, self)


class AbsentContext(RecordedContextState):
    def next_recording_turn(self) -> int:
        return 1

    def require_newest(
        self, db: sqlite3.Connection, source: ContextStoreIdentity, owner: ThreadIncarnation
    ) -> None:
        if ContextRequest.newest(db, owner):
            raise ContextPriorSupersededError(
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
            raise ContextPriorSupersededError(
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


class ContextManifestCursor(ABC):
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
class PositionedContextManifest(ContextManifestCursor):
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


class BeforeContextManifest(PositionedContextManifest):
    comparison = "<"
    descending = True


class AfterContextManifest(PositionedContextManifest):
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
class ContextStore(DurableStore):
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
        if rows:
            return self.resolve(db, source, rows[0])
        return AbsentContext()

    def entries(
        self, identity: NativeSessionIdentity, entry_ids: Iterable[NativeItemId],
    ) -> dict[NativeItemId, SegmentManifest]:
        """The recorded manifest of each named journal entry that has one."""
        with self.snapshot() as db:
            return {row.entry_id: row.manifest for entry_id in entry_ids
                    for row in ContextEntry.select(db, session_id=identity.session_id,
                                                   session_file=identity.session_file,
                                                   entry_id=entry_id)}

    def prior(self, owner: ThreadIncarnation) -> RecordedContextState:
        """Read the thread's previous request from a snapshot, outside any write lock."""
        with self.snapshot() as db:
            return ContextStoreInfo.source_in(db).latest(self, db, owner)

    def record_after(self, prior: ContextPrior, manifest: ContextManifest) -> CapturedContext:
        """Append ``manifest`` after ``prior``; the result is the next request's prior,
        so a writer holding its last request never resolves it again."""
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
        row: ContextRequest,
    ) -> CapturedContext:
        records = {row.id: row.recorded(db)}
        references: set[ContextEntryReference] = set()
        for position, part in enumerate(records[row.id].segments):
            references.update(part.entries)
            for predecessor in part.predecessors(row.id):
                if predecessor not in records:
                    prior = ContextRequest.require(db, id=predecessor)
                    if prior.identity.thread != row.identity.thread:
                        raise RelationViolationError("Context predecessor is missing or foreign")
                    records[predecessor] = prior.recorded(db)
                # Only the referenced segment is needed, not the predecessor
                # request's other segments and their unrelated dependencies.
                prior_part = part.predecessor(records, position)
                references.update(prior_part.entries)
        entries = {ref: ContextEntry.original(db, ref) for ref in references}
        record = records[row.id]
        return CapturedContext(source, row.id, record, record.restore(entries, records))

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
