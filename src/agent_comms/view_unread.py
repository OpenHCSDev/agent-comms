"""Human read positions for native transcripts, independent of inbox delivery."""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, RLock
from typing import TYPE_CHECKING, ClassVar, Annotated
from weakref import WeakValueDictionary, finalize

from .coordination_errors import CoordinationReadUnavailable
from .locked_store import PublishedSQLiteFile, RuntimeFile, SQLiteFile
from .store_files import StoreBusy
from .bus import Bus
from .native_entries import NativeEntry
from .native_transcript import SessionCut, reverse_records
from .private_path import FileObservation, FileRevision, MissingFile
from .read_ledger import ReadDocument, ReadLedger
from .registry_document import RosterVisibility
from .thread_owned_state import ThreadOwnedState
from .typed_table import (
    Column,
    JsonStorage,
    SQLiteTableName,
    SQLiteUserVersion,
    TypedRow,
    TypedTable,
    WithoutRowid,
)
from .ui_model.unread import CappedReplyUnread, IndexingUnread, ReplyUnread, UnreadPresentation
from .watched import FileSource

if TYPE_CHECKING:
    from .agent_backend import RuntimeLocation
    from .registry_document import RegistrySnapshot
    from .thread_identity import ThreadIncarnation
    from .threads import Thread

_INDEX_VERSION = 7


class ReplyIndexTable:
    """Tables belonging to this disposable transcript reply index."""


@dataclass(frozen=True)
class ReplyIndex(ReplyIndexTable, TypedTable):
    revision: Annotated[FileObservation, Column(storage=JsonStorage, nullable=True)] = MissingFile()
    records: int = 0
    source: Annotated[str, Column(primary_key=True, check="source<>''")] = ""
    start: int = 0
    limit: ClassVar[int] = 32

    @classmethod
    def for_sources(
        cls, database: sqlite3.Connection, sources: Sequence[str]
    ) -> dict[str, ReplyIndex]:
        rows = cls.read(database.execute(
            f"SELECT * FROM {cls.declared_name} WHERE source IN (SELECT value FROM json_each(?))",
            (json.dumps(tuple(sources)),),
        ))
        indexes = {source: cls(source=source) for source in sources}
        indexes.update((row.source, row) for row in rows)
        return indexes

    def counts(self, revision: FileRevision, seen: int) -> bool:
        """The read reached this mark or exhausted the record allowance."""
        return seen == revision.size or (
            self.revision == revision and (self.start <= seen or self.records >= self.limit)
        )

    def continues(self, revision: FileRevision) -> bool:
        return self.revision == revision

    def counted(self, total: int, seen: int) -> Counted:
        """Coverage, not the number found, determines whether this is a lower bound."""
        return CappedCount(total) if self.start > seen else Counted(total)

    def prepare(self, revision: FileRevision, seen: int, cancelled: Event) -> ReplyIndexResult:
        """Visit at most limit newest records, stopping earlier at the read mark."""
        resume = self.continues(revision)
        start, visited = (self.start, self.records) if resume else (revision.size, 0)
        replies: list[TranscriptReply] = []
        with Path(self.source).open("rb") as stream:
            if not revision.opened_by(stream):
                return RevisionMoved()
        records = reverse_records(Path(self.source), start, floor=max(0, seen - 1))
        with closing(records):
            while visited < self.limit and start > seen:
                if cancelled.is_set():
                    return CancelledRead()
                record = next(records, None)
                if record is None:
                    start = seen
                    break
                begin, end, raw = record
                visited += 1
                start = begin
                if raw.endswith(b"\n") and NativeEntry.read(raw).unread_reply:
                    replies.append(TranscriptReply(self.source, end))
        index = ReplyIndex(revision=revision, records=visited, source=self.source, start=start)
        return ReplyIndexUpdate(self, index, tuple(replies))


@dataclass(frozen=True)
class ReplyRead:
    """The captured source and viewer mark authorized for one worker read."""
    index: ReplyIndex
    revision: FileRevision
    seen: int

    def prepare(self, cancelled: Event) -> ReplyIndexResult:
        """One independently scheduled source read; no loop over other people's files."""
        try:
            return self.index.prepare(self.revision, self.seen, cancelled)
        except FileNotFoundError:
            return RevisionMoved()


@dataclass(frozen=True)
class TranscriptReply(ReplyIndexTable, WithoutRowid):
    source: Annotated[str, Column(primary_key=True)]
    end: Annotated[int, Column(primary_key=True)]

    @classmethod
    def after(cls, database: sqlite3.Connection, ends: Mapping[str, int]) -> dict[str, int]:
        """Count replies after each mark within the index owner's admitted records."""
        rows = _ReplyCount.read(database.execute(
            f'''SELECT displayed.key AS source,
                (SELECT count(*) FROM {cls.declared_name}
                 WHERE source=displayed.key AND end>displayed.value) AS total
                FROM json_each(?) AS displayed''',
            (json.dumps(ends),),
        ))
        return {row.source: row.total for row in rows}


@dataclass(frozen=True)
class _ReplyCount(TypedRow):
    source: str
    total: int


class FileReplyCount(ABC):
    @abstractmethod
    def __add__(self, other: FileReplyCount) -> FileReplyCount: ...

    @abstractmethod
    def added(self, total: int) -> FileReplyCount: ...

    @abstractmethod
    def capped(self, total: int) -> FileReplyCount: ...

    @abstractmethod
    def record(self, name: str, counts: dict[str, UnreadPresentation]) -> None: ...


@dataclass(frozen=True)
class Counted(FileReplyCount):
    total: int

    def __add__(self, other: FileReplyCount) -> FileReplyCount:
        return other.added(self.total)

    def added(self, total: int) -> FileReplyCount:
        return replace(self, total=self.total + total)

    def capped(self, total: int) -> FileReplyCount:
        return CappedCount(self.total + total)

    def record(self, name: str, counts: dict[str, UnreadPresentation]) -> None:
        counts[name] = ReplyUnread(self.total)


@dataclass(frozen=True)
class CappedCount(Counted):
    def __add__(self, other: FileReplyCount) -> FileReplyCount:
        return other.capped(self.total)

    def record(self, name: str, counts: dict[str, UnreadPresentation]) -> None:
        counts[name] = CappedReplyUnread.for_count(self.total)


@dataclass(frozen=True)
class Indexing(FileReplyCount):
    def __add__(self, other: FileReplyCount) -> FileReplyCount:
        return self

    def added(self, total: int) -> FileReplyCount:
        return self

    def capped(self, total: int) -> FileReplyCount:
        return self

    def record(self, name: str, counts: dict[str, UnreadPresentation]) -> None:
        counts[name] = IndexingUnread()


@dataclass(frozen=True)
class TranscriptUnread:
    """Per-thread reply rows: exact, a record-capped lower bound, or still preparing."""

    counts: dict[str, UnreadPresentation]


class ReplyIndexResult(ABC):
    """A prepared delta or the specific reason no delta could be prepared."""

    @abstractmethod
    def commit(self, database: sqlite3.Connection) -> None: ...


class NoReplyUpdate(ReplyIndexResult):
    def commit(self, database: sqlite3.Connection) -> None:
        pass


class CancelledRead(NoReplyUpdate):
    """The owning reader was retired; publish nothing from its cancelled read."""


class RevisionMoved(NoReplyUpdate):
    """The opened file no longer matches the cut; observe a fresh revision."""


@dataclass(frozen=True)
class ReplyIndexUpdate(ReplyIndexResult):
    """A native-only prepared delta, authorized against one stored suffix."""

    before: ReplyIndex
    after: ReplyIndex
    replies: tuple[TranscriptReply, ...]

    def commit(self, database: sqlite3.Connection) -> None:
        current = ReplyIndex.one(database, source=self.after.source) or ReplyIndex(
            source=self.after.source
        )
        if current != self.before:
            return  # Another observer changed this suffix; discard the stale delta.
        if not self.before.continues(self.after.revision):
            database.execute(
                f"DELETE FROM {TranscriptReply.declared_name} WHERE source=?", (self.after.source,)
            )
        for reply in self.replies:
            reply.insert(database)
        self.after.upsert(database)


class ReplyPreparation:
    """Own source reads, their publication and completion subscriptions."""

    def commit(self, database: sqlite3.Connection, wanted: Collection[str]) -> None:
        pass

    def cancel(self) -> None:
        pass

    def retire(self, sources: Collection[str]) -> None:
        pass

    def retain(self, sources: Collection[str]) -> None:
        pass

    def finished(
        self, state: TranscriptReadState, database: sqlite3.Connection,
        wanted: Collection[str],
    ) -> ReplyPreparation:
        return self

    def started(
        self, state: TranscriptReadState,
        needed: dict[str, ReplyRead],
    ) -> ReplyPreparation:
        return self

    def watch(
        self, previous: frozenset[Future[ReplyIndexResult]],
        on_done: Callable[[Future[ReplyIndexResult]], None],
    ) -> frozenset[Future[ReplyIndexResult]]:
        return frozenset()


class IdleReplyPreparation(ReplyPreparation):
    """No authorized source reads and no worker-completion subscription."""

    def started(
        self, state: TranscriptReadState,
        needed: dict[str, ReplyRead],
    ) -> ReplyPreparation:
        if not needed or not state._cleanup.alive:
            return self
        return NativeReplyPreparation({}).started(state, needed)


@dataclass(frozen=True)
class ReplyReading:
    """One authorized source and its independently completing reader."""

    read: ReplyRead
    future: Future[ReplyIndexResult]

    def commit(self, database: sqlite3.Connection) -> None:
        if self.future.cancelled():
            return
        expected = self.read.revision
        revision = FileRevision.capture(Path(self.read.index.source))
        if revision.contains_cut(expected, expected.size):
            self.future.result().commit(database)


@dataclass
class NativeReplyPreparation(ReplyPreparation):
    """Publish completed sources without waiting for another source's reader."""

    readers: dict[str, ReplyReading]

    def started(self, state: TranscriptReadState, needed: dict[str, ReplyRead]) -> ReplyPreparation:
        if state._cleanup.alive:
            for source, read in needed.items():
                if source not in self.readers:
                    self.readers[source] = ReplyReading(
                        read, state._pool.submit(read.prepare, state._cancelled)
                    )
        return self

    def cancel(self) -> None:
        self.retire(tuple(self.readers))

    def retain(self, sources: Collection[str]) -> None:
        self.retire(self.readers.keys() - set(sources))

    def retire(self, sources: Collection[str]) -> None:
        for source in sources:
            if (reader := self.readers.pop(source, None)) is not None:
                reader.future.cancel()

    def finished(
        self, state: TranscriptReadState, database: sqlite3.Connection,
        wanted: Collection[str],
    ) -> ReplyPreparation:
        self.retain(wanted)
        completed = tuple(source for source, reader in self.readers.items() if reader.future.done())
        if not completed:
            return self
        # The state lock owns local retirement; SQLite owns publication exclusion.
        # A busy writer leaves these exact completed readers for the next observation.
        try:
            with (CoordinationReadUnavailable.checking(),
                  state.publishing_changes(database, state.index_path),
                  SQLiteFile.write_transaction(database)):
                for source in completed:
                    self.readers[source].commit(database)
        except CoordinationReadUnavailable:
            return self
        for source in completed:
            del self.readers[source]
        return self if self.readers else state._idle

    def watch(
        self, previous: frozenset[Future[ReplyIndexResult]],
        on_done: Callable[[Future[ReplyIndexResult]], None],
    ) -> frozenset[Future[ReplyIndexResult]]:
        current = frozenset(reader.future for reader in self.readers.values())
        for future in current - previous:
            future.add_done_callback(on_done)
        return current


@dataclass(frozen=True)
class TranscriptReadKeys:
    incarnation: ThreadIncarnation
    runtime: RuntimeLocation
    revisions: dict[str, FileObservation]
    paths: dict[str, Path]

    def matches(self, thread: Thread) -> bool:
        return self.incarnation == thread.incarnation and self.runtime == thread.runtime

    @classmethod
    def capture(cls, thread: Thread):
        return cls(thread.incarnation, thread.runtime, {}, {}).refresh({})

    def refresh(self, observed: Mapping[str, FileObservation]) -> TranscriptReadKeys:
        paths = {key: self.paths[key] if key in self.paths else Path(key).absolute()
                 for key in self.runtime.read_keys()}
        return replace(self, paths=paths, revisions={
            key: observed[key] if key in observed else FileRevision.capture(path)
            for key, path in paths.items()
        })


class TranscriptReadState(RuntimeFile, PublishedSQLiteFile, ThreadOwnedState):
    """Index only replies after read marks, capped from the newest records.

    One disposable suffix index owns reply offsets and scan coverage. No worker
    finishes an irrelevant prefix; source replacement discards its projection.
    """

    filename = f"transcript_reply_index.v{_INDEX_VERSION}.sqlite3"

    def __init__(self, path: Path):
        self._thread_keys: dict[str, TranscriptReadKeys] = {}
        self._path_threads: dict[Path, set[str]] = {}
        self._key_snapshot: RegistrySnapshot | None = None
        self._source_resources = ExitStack()
        self._source_watch = self._source_resources.enter_context(Bus.register_sources())
        self.path = ReadLedger.path_in(path.parent)
        self.reads = ReadLedger(self.path)
        self.index_path = self.path_in(path.parent)
        self._connection: sqlite3.Connection | None = None
        self._database_inode: int | None = None
        self._lock = RLock()
        self._cancelled = Event()
        self._idle = IdleReplyPreparation()
        self._preparation: ReplyPreparation = self._idle
        self._database_resources = ExitStack()
        self._resources = ExitStack()
        self._resources.callback(self._database_resources.close)
        self._resources.callback(self._source_resources.close)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="reply-read")
        self._resources.callback(self._pool.shutdown, wait=True, cancel_futures=True)
        self._resources.callback(self._cancelled.set)
        # Capture the resource stack, not this state: dead roots must remain collectible.
        self._cleanup = finalize(self, self._resources.close)

    def close(self) -> None:
        """Retire the worker and SQLite through their shared resource lifetime."""
        with self._lock:
            self._preparation.cancel()
            self._cleanup()
            self._preparation = self._idle
            self._close_database()

    @property
    def preparation(self) -> ReplyPreparation:
        """The source reads currently owned by this state."""
        return self._preparation

    def watch(self, previous: frozenset[Future[ReplyIndexResult]],
              on_done: Callable[[Future[ReplyIndexResult]], None]
              ) -> frozenset[Future[ReplyIndexResult]]:
        """Subscribe against one stable preparation while other readers may finish."""
        with self._lock:
            return self._preparation.watch(previous, on_done)

    def _close_database(self) -> None:
        self._database_resources.close()
        self._connection = None
        self._database_inode = None

    def _database(self) -> sqlite3.Connection:
        revision = FileRevision.capture(self.index_path)
        if self._connection is not None and (
            not revision.holds_inode(self._database_inode)
        ):
            self._close_database()
        if self._connection is None:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            connection = self.writer(
                self.index_path, StoreBusy(), check_same_thread=False
            )
            try:
                connection.execute("PRAGMA synchronous=NORMAL")
                (version,) = SQLiteUserVersion.read(connection.execute("PRAGMA user_version"))
                # Opening an installed index is a read. Only initialization
                # needs a writer; recheck after taking it because another
                # process may have initialized the file since this read.
                if version.user_version != _INDEX_VERSION:
                    with (self.publishing_changes(connection, self.index_path),
                          SQLiteFile.write_transaction(connection)):
                        (version,) = SQLiteUserVersion.read(connection.execute("PRAGMA user_version"))
                        if version.user_version == 0:
                            if SQLiteTableName.stored(connection):
                                raise ValueError("Reply index requires the quiet runtime reset")
                            for table in TypedTable.members_with(ReplyIndexTable):
                                table.create(connection)
                            connection.execute(f"PRAGMA user_version={_INDEX_VERSION}")
                        elif version.user_version != _INDEX_VERSION:
                            raise ValueError("Reply index requires the quiet runtime reset")
            except (sqlite3.DatabaseError, ValueError):
                connection.close()
                raise
            self._connection = self._database_resources.enter_context(closing(connection))
            self._database_inode = self.index_path.stat().st_ino
        return self._connection

    def _discard_database(self) -> None:
        self._close_database()
        for path in self.files(self.index_path):
            path.unlink(missing_ok=True)
        self.publish_change(self.index_path)

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        """Drop the reply projection of each deleted thread's transcript."""
        sources = set()
        for thread in threads:
            sources.update(thread.runtime.read_keys())
        if not sources:
            return
        with self._lock:
            self._preparation.retire(sources)
            if not self.index_path.exists():
                return
            with (closing(self.writer(self.index_path)) as database,
                  self.publishing_changes(database, self.index_path), database):
                for table in TypedTable.members_with(ReplyIndexTable):
                    database.executemany(
                        f"DELETE FROM {table.declared_name} WHERE source = ?",
                        ((source,) for source in sources),
                    )

    @contextmanager
    def _indexed_sources(
        self, revisions: dict[str, FileObservation], seen: Mapping[str, int]
    ) -> Iterator[tuple[sqlite3.Connection, dict[str, ReplyIndex]]]:
        database = self._database()
        # Publish before reading committed counts, if the preparation has finished.
        self._preparation = self._preparation.finished(self, database, revisions)
        with database:
            self.begin_snapshot(database)
            indexes = ReplyIndex.for_sources(database, tuple(revisions))
            needed = {
                source: ReplyRead(indexes[source], revision, seen[source])
                for source, observed in revisions.items()
                for revision in observed.revisions()
                if not indexes[source].counts(revision, seen[source])
            }
            yield database, indexes
        # Start only after releasing the read snapshot. Spawn receives data and
        # cancellation, never this connection, the observer pipe or the owner's locks.
        self._preparation = self._preparation.started(self, needed)

    def _refresh_keys(self, snapshot: RegistrySnapshot) -> None:
        changed = self._source_watch.changed_paths()
        # Visibility selects counts, not the lifetime of a registered source.
        rebuild = False
        if snapshot is not self._key_snapshot:
            current = {}
            for thread in snapshot.visible_threads(RosterVisibility(stopped=True, archived=True)):
                prior = self._thread_keys.get(thread.name)
                keys = (prior if prior is not None and prior.matches(thread)
                        else TranscriptReadKeys.capture(thread))
                rebuild |= prior is None or prior.revisions.keys() != keys.revisions.keys()
                current[thread.name] = keys
            rebuild |= bool(self._thread_keys.keys() - current.keys())
            self._thread_keys = current
            self._key_snapshot = snapshot
        names = {name for path in changed for name in self._path_threads.get(path, ())}
        # A queued event can belong to a source removed by this registry revision.
        for name in names & self._thread_keys.keys():
            keys = self._thread_keys[name]
            observed = {key: FileRevision.capture(path) for key, path in keys.paths.items()}
            if observed != keys.revisions:
                current_keys = keys.refresh(observed)
                rebuild |= keys.revisions.keys() != current_keys.revisions.keys()
                self._thread_keys[name] = current_keys
        if rebuild:
            self._path_threads = {}
            for name, keys in self._thread_keys.items():
                for path in keys.paths.values():
                    self._path_threads.setdefault(path, set()).add(name)
        if rebuild:
            self._source_resources.close()
            sources = tuple(FileSource(path) for path in self._path_threads)
            wanted = {key for keys in self._thread_keys.values() for key in keys.revisions}
            self._preparation.retain(wanted)
            if not sources:
                self._preparation.cancel()
                self._preparation = self._idle
            self._source_watch = self._source_resources.enter_context(
                Bus.register_sources(*sources)
            )
            # Cover a write between the first capture and installing its watch.
            for name, keys in tuple(self._thread_keys.items()):
                observed = {key: FileRevision.capture(path) for key, path in keys.paths.items()}
                if observed != keys.revisions:
                    self._thread_keys[name] = keys.refresh(observed)

    def counts(self, viewer: str, *, registry: RegistrySnapshot, document: ReadDocument,
               visibility: RosterVisibility) -> TranscriptUnread:
        # Counts never wait for native decoding or another in-process reader.
        if not self._lock.acquire(blocking=False):
            raise CoordinationReadUnavailable("Transcript counts are being read")
        try:
            if not self._cleanup.alive:
                raise RuntimeError("Transcript reader is closed")
            self._refresh_keys(registry)
            keys = {name: keys for name, keys in self._thread_keys.items()
                    if registry.status(name).in_view(visibility)}
            try:
                return self._counts_locked(viewer, document, keys)
            except sqlite3.DatabaseError as error:
                if error.sqlite_errorcode not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                    CoordinationReadUnavailable.raise_from(error)
                self._discard_database()
                return self._counts_locked(viewer, document, keys)
        finally:
            self._lock.release()

    def _counts_locked(self, viewer: str, document: ReadDocument,
                       threads: Mapping[str, TranscriptReadKeys]) -> TranscriptUnread:
        revisions = {source: revision for keys in threads.values()
                     for source, revision in keys.revisions.items()}
        if not revisions:
            return TranscriptUnread({name: ReplyUnread(0) for name in threads})
        # A missing transcript has no replies.
        per_file: dict[str, FileReplyCount] = {source: Counted(0) for source in revisions}
        present = {source: revision for source, observed in revisions.items()
                   for revision in observed.revisions()}
        seen = {}
        for source, revision in present.items():
            mark = self.reads.transcript_seen(
                viewer, source, revision.identity.inode, document=document
            )
            seen[source] = mark if mark <= revision.size else 0
        with self._indexed_sources(revisions, seen) as (database, indexes):
            ends: dict[str, int] = {}
            for source, revision in present.items():
                if seen[source] == revision.size:
                    per_file[source] = Counted(0)
                elif not indexes[source].counts(revision, seen[source]):
                    per_file[source] = Indexing()
                else:
                    ends[source] = seen[source]
            for source, count in TranscriptReply.after(database, ends).items():
                per_file[source] = indexes[source].counted(count, seen[source])
        result: dict[str, UnreadPresentation] = {}
        for name, keys in threads.items():
            total = sum((per_file[source] for source in keys.revisions), Counted(0))
            total.record(name, result)
        return TranscriptUnread(result)

    def mark_read(self, viewer: str, source: str, through: int, cut: SessionCut) -> None:
        cut.mark_read(self.reads, viewer, source, through)


_shared_lock = RLock()
_shared_states: WeakValueDictionary[str, TranscriptReadState] = WeakValueDictionary()


def transcript_read_state(path: Path) -> TranscriptReadState:
    """Reuse one source index per live wire root, without retaining dead roots."""
    key = str(path.expanduser().resolve())
    with _shared_lock:
        state = _shared_states.get(key)
        if state is None or not state._cleanup.alive:
            state = TranscriptReadState(Path(key))
            _shared_states[key] = state
        return state
