"""Human read positions for native transcripts, independent of inbox delivery."""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from multiprocessing.synchronize import Event
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, ClassVar
from weakref import WeakValueDictionary, finalize

from .coordination_database import CoordinationStore, StoreBusy
from .coordination_errors import CoordinationReadUnavailable
from .launch_runtime import ThreadLaunch
from .native_entries import NativeEntry
from .native_transcript import SessionCut, _reverse_records
from .private_path import FileRevision
from .read_ledger import ReadDocument, ReadLedger
from .registry_document import RosterVisibility
from .store_files import SharedLock, _store_lock
from .thread_owned_state import ThreadOwnedState
from .typed_table import Column, SQLiteUserVersion, TypedRow, TypedTable
from .wire_watch import FileWatch, NativeFileWatch, WatchSource

if TYPE_CHECKING:
    from .threads import Thread
    from .registration import Registration
    from .agent_backend import RuntimeLocation
    from .thread_identity import ThreadIncarnation

_INDEX_VERSION = 5


class ReplyIndexTable:
    """Tables belonging to this disposable transcript reply index."""


@dataclass(frozen=True)
class ReplyIndex(ReplyIndexTable, TypedTable):
    revision: FileRevision | None = None
    through: int = 0
    total: int = 0
    source: str = field(default="", metadata={"sql": Column(primary_key=True, check="source<>''")})
    start: int = 0
    limit: ClassVar[int] = 100

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
        """A suffix covers this mark, or already proves the declared cap."""
        return seen == revision.size or (
            self.revision == revision and (self.start <= seen or self.total >= self.limit)
        )

    def continues(self, revision: FileRevision) -> bool:
        return self.revision == revision

    def prepare(self, revision: FileRevision, budget: IndexSlice, seen: int) -> ReplyIndexResult:
        """Read newest-first only to this viewer's mark or the reply cap."""
        if not budget.available:
            return BudgetSpent()
        resume = self.continues(revision)
        start, total = (self.start, self.total) if resume else (revision.size, 0)
        replies: list[TranscriptReply] = []
        with Path(self.source).open("rb") as stream:
            if not revision.opened_by(stream):
                return RevisionMoved()
        records = _reverse_records(Path(self.source), start, floor=max(0, seen - 1))
        with closing(records):
            for _ in range(budget.source_records):
                if not budget.available or total >= self.limit or start <= seen:
                    break
                record = next(records, None)
                if record is None:
                    start = seen
                    break
                begin, end, raw = record
                budget.consumed(len(raw))
                start = begin
                if raw.endswith(b"\n") and NativeEntry.read(raw).unread_reply:
                    total += 1
                    replies.append(TranscriptReply(self.source, end))
        index = ReplyIndex(revision, revision.size, total, self.source, start)
        return ReplyIndexUpdate(self, index, tuple(replies))


@dataclass(frozen=True)
class TranscriptReply(ReplyIndexTable, TypedTable):
    source: str = field(metadata={"sql": Column(primary_key=True)})
    end: int = field(metadata={"sql": Column(primary_key=True)})
    without_rowid = True

    @classmethod
    def after(cls, database: sqlite3.Connection, ends: Mapping[str, int]) -> dict[str, int]:
        """Count the indexed suffix, bounded by the owner's display cap."""
        rows = _ReplyCount.read(database.execute(
            f'''SELECT displayed.key AS source,
                (SELECT count(*) FROM (SELECT end FROM {cls.declared_name}
                 WHERE source=displayed.key AND end>displayed.value LIMIT ?)) AS total
                FROM json_each(?) AS displayed''',
            (ReplyIndex.limit, json.dumps(ends)),
        ))
        return {row.source: row.total for row in rows}


@dataclass(frozen=True)
class _ReplyCount(TypedRow):
    source: str
    total: int


@dataclass(frozen=True)
class _IndexTable(TypedRow):
    name: str


class FileReplyCount(ABC):
    @abstractmethod
    def __add__(self, other: FileReplyCount) -> FileReplyCount: ...

    @abstractmethod
    def added(self, total: int) -> FileReplyCount: ...

    @abstractmethod
    def record(self, name: str, counts: dict[str, int], pending: set[str]) -> None: ...


@dataclass(frozen=True)
class Counted(FileReplyCount):
    total: int

    def __add__(self, other: FileReplyCount) -> FileReplyCount:
        return other.added(self.total)

    def added(self, total: int) -> FileReplyCount:
        return Counted(self.total + total)

    def record(self, name: str, counts: dict[str, int], pending: set[str]) -> None:
        counts[name] = self.total


@dataclass(frozen=True)
class Indexing(FileReplyCount):
    def __add__(self, other: FileReplyCount) -> FileReplyCount:
        return self

    def added(self, total: int) -> FileReplyCount:
        return self

    def record(self, name: str, counts: dict[str, int], pending: set[str]) -> None:
        pending.add(name)


@dataclass(frozen=True)
class TranscriptUnread:
    """Exact or capped suffix counts; pending names await their requested slice."""

    counts: dict[str, int]
    pending: frozenset[str]


@dataclass
class IndexSlice:
    """Cooperative work budget, checked between native records.

    One record is indivisible: it may exceed the byte/time slice but is never
    skipped or truncated. The next refresh resumes after that record.
    """

    seconds: ClassVar[float] = 0.15
    source_records: ClassVar[int] = 256
    _worker_cancellation: ClassVar[Event]

    deadline: float
    cancelled: Event
    bytes_left: int = 1024 * 1024
    records_left: int = 512

    @classmethod
    def initialize_worker(cls, cancelled: Event) -> None:
        """Bind the parent's cancellation signal, never its agent identity or stores."""
        ThreadLaunch.strip(os.environ)
        cls._worker_cancellation = cancelled

    @classmethod
    def worker_slice(cls) -> IndexSlice:
        return cls(monotonic() + cls.seconds, cls._worker_cancellation)

    @property
    def available(self) -> bool:
        return (
            not self.cancelled.is_set()
            and monotonic() < self.deadline
            and min(self.bytes_left, self.records_left) > 0
        )

    def consumed(self, size: int) -> None:
        self.bytes_left -= size
        self.records_left -= 1


class ReplyIndexResult(ABC):
    """A prepared delta or the specific reason no delta could be prepared."""

    @abstractmethod
    def collect(self, updates: list[ReplyIndexUpdate]) -> None: ...


class NoReplyUpdate(ReplyIndexResult):
    def collect(self, updates: list[ReplyIndexUpdate]) -> None:
        pass


class BudgetSpent(NoReplyUpdate):
    """No slice remains; leave the stored prefix for the next batch."""


class RevisionMoved(NoReplyUpdate):
    """The opened file no longer matches the cut; observe a fresh revision."""


@dataclass(frozen=True)
class ReplyIndexUpdate(ReplyIndexResult):
    """A native-only prepared delta, authorized against one stored prefix."""

    before: ReplyIndex
    after: ReplyIndex
    replies: tuple[TranscriptReply, ...]

    def collect(self, updates: list[ReplyIndexUpdate]) -> None:
        updates.append(self)

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


class ReplyResume:
    """The committed resume position orders the next source reads."""

    def order(
        self, needed: Mapping[str, tuple[ReplyIndex, FileRevision, int]],
    ) -> tuple[tuple[ReplyIndex, FileRevision, int], ...]:
        return tuple(needed.values())


@dataclass(frozen=True)
class ResumeFromStart(ReplyResume):
    """No source has been visited; use the sources' natural order."""


@dataclass(frozen=True)
class ResumeAfter(ReplyResume):
    source: str

    def order(
        self, needed: Mapping[str, tuple[ReplyIndex, FileRevision, int]],
    ) -> tuple[tuple[ReplyIndex, FileRevision, int], ...]:
        names = tuple(needed)
        start = names.index(self.source) + 1 if self.source in needed else 0
        reads = super().order(needed)
        return (*reads[start:], *reads[:start])


@dataclass(frozen=True)
class ReplyIndexBatch:
    """The compact result of one bounded, fair native-file preparation unit."""

    updates: tuple[ReplyIndexUpdate, ...]
    resume: ReplyResume

    @classmethod
    def prepare(cls, reads: tuple[tuple[ReplyIndex, FileRevision, int], ...]) -> ReplyIndexBatch:
        budget = IndexSlice.worker_slice()
        updates: list[ReplyIndexUpdate] = []
        resume: ReplyResume = ResumeFromStart()
        for index, revision, seen in reads:
            if not budget.available:
                break
            resume = ResumeAfter(index.source)
            try:
                index.prepare(revision, budget, seen).collect(updates)
            except FileNotFoundError:
                continue  # A later source observation sees the missing file.
        return cls(tuple(updates), resume)


class ReplyPreparation:
    """Own whether an index batch can start, publish or wake an observer."""

    future: Future[ReplyIndexBatch]

    @property
    def pending(self) -> bool:
        return not self.future.done()

    def commit(self, database: sqlite3.Connection, wanted: Collection[str]) -> ReplyResume:
        return ResumeFromStart()

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
        needed: dict[str, tuple[ReplyIndex, FileRevision, int]],
    ) -> ReplyPreparation:
        return self

    def read_counts(
        self, state: TranscriptReadState, viewer: str,
        document: ReadDocument,
    ) -> TranscriptUnread:
        return state._counts_locked(viewer, document)

    def watch(
        self, previous: Future[ReplyIndexBatch],
        on_done: Callable[[Future[ReplyIndexBatch]], None],
    ) -> Future[ReplyIndexBatch]:
        return previous


_EMPTY_REPLY_FUTURE: Future[ReplyIndexBatch] = Future()
_EMPTY_REPLY_FUTURE.set_result(ReplyIndexBatch((), ResumeFromStart()))


class IdleReplyPreparation(ReplyPreparation):
    """A completed empty future, shared without creating a worker or a callback."""

    future = _EMPTY_REPLY_FUTURE

    def started(
        self, state: TranscriptReadState,
        needed: dict[str, tuple[ReplyIndex, FileRevision, int]],
    ) -> ReplyPreparation:
        if not needed or not state._cleanup.alive:
            return self
        return NativeReplyPreparation(
            needed, state._pool.submit(ReplyIndexBatch.prepare, state._resume.order(needed))
        )

    def read_counts(
        self, state: TranscriptReadState, viewer: str,
        document: ReadDocument,
    ) -> TranscriptUnread:
        return super().read_counts(state, viewer, document)


@dataclass
class NativeReplyPreparation(ReplyPreparation):
    """The actual authorized work and future; deletion withdraws its original reads."""

    reads: dict[str, tuple[ReplyIndex, FileRevision, int]]
    future: Future[ReplyIndexBatch]

    def cancel(self) -> None:
        self.future.cancel()

    def retain(self, sources: Collection[str]) -> None:
        self.retire(self.reads.keys() - set(sources))

    def retire(self, sources: Collection[str]) -> None:
        for source in sources:
            self.reads.pop(source, None)
        if not self.reads:
            self.future.cancel()

    def finished(
        self, state: TranscriptReadState, database: sqlite3.Connection,
        wanted: Collection[str],
    ) -> ReplyPreparation:
        if self.pending:
            return self
        # A busy writer retains this exact work; it does not invalidate other
        # sources. The wire lock orders publication against source deletion.
        try:
            with _store_lock(state.path.parent / "wire", mode=SharedLock(), blocking=False):
                state._resume = self.commit(database, wanted)
        except (BlockingIOError, CoordinationReadUnavailable):
            return self
        return state._idle

    def watch(
        self, previous: Future[ReplyIndexBatch],
        on_done: Callable[[Future[ReplyIndexBatch]], None],
    ) -> Future[ReplyIndexBatch]:
        if self.future is not previous:
            self.future.add_done_callback(on_done)
        return self.future

    def commit(self, database: sqlite3.Connection, wanted: Collection[str]) -> ReplyResume:
        if self.future.cancelled():
            return ResumeFromStart()
        batch = self.future.result()
        with CoordinationReadUnavailable.checking():
            with database:
                database.execute("BEGIN IMMEDIATE")
                for update in batch.updates:
                    source = update.after.source
                    if source not in self.reads or source not in wanted:
                        continue
                    revision = FileRevision.capture(Path(source))
                    expected = self.reads[source][1]
                    if revision is not None and expected.holds_cut(revision, expected.size):
                        update.commit(database)
        return batch.resume


@dataclass(frozen=True)
class TranscriptReadKeys:
    incarnation: ThreadIncarnation
    runtime: RuntimeLocation
    revisions: dict[str, FileRevision | None]

    def matches(self, thread: Thread) -> bool:
        return self.incarnation == thread.incarnation and self.runtime == thread.runtime

    @classmethod
    def capture(cls, thread: Thread, observed: Mapping[str, FileRevision | None]):
        return cls(thread.incarnation, thread.runtime, {
            key: observed[key] if key in observed else FileRevision.capture(Path(key))
            for key in thread.runtime.read_keys()
        })


class TranscriptReadState(ThreadOwnedState):
    """Index only replies after read marks, capped from the newest records.

    One disposable suffix index owns reply offsets and scan coverage. No worker
    finishes an irrelevant prefix; source replacement discards its projection.
    """

    def __init__(self, path: Path, registry: Registration):
        self.registry = registry
        self._thread_keys: dict[str, TranscriptReadKeys] = {}
        self._source_watch: FileWatch = NativeFileWatch.open()
        self._source_resources = ExitStack()
        self.path = ReadLedger.path_in(path.parent)
        self.reads = ReadLedger(self.path)
        self.index_path = path.with_name(f"transcript_reply_index.v{_INDEX_VERSION}.sqlite3")
        self._connection: sqlite3.Connection | None = None
        self._database_inode: int | None = None
        self._lock = RLock()
        self._context = multiprocessing.get_context("spawn")
        self._cancelled = self._context.Event()
        self._resume: ReplyResume = ResumeFromStart()
        self._idle = IdleReplyPreparation()
        self._preparation: ReplyPreparation = self._idle
        self._database_resources = ExitStack()
        self._resources = ExitStack()
        self._resources.callback(self._database_resources.close)
        self._resources.callback(self._source_resources.close)
        # Constructing the executor does not start a worker; the first submit does.
        self._pool = ProcessPoolExecutor(
            max_workers=1, mp_context=self._context,
            initializer=IndexSlice.initialize_worker, initargs=(self._cancelled,),
        )
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
        """The authorized completion or its idle member for the observer's wakeup."""
        return self._preparation

    def _close_database(self) -> None:
        self._database_resources.close()
        self._connection = None
        self._database_inode = None

    def _database(self) -> sqlite3.Connection:
        revision = FileRevision.capture(self.index_path)
        if self._connection is not None and (
            revision is None or revision.identity.inode != self._database_inode
        ):
            self._close_database()
        if self._connection is None:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            connection = CoordinationStore.writer(
                self.index_path, StoreBusy(), check_same_thread=False
            )
            try:
                connection.execute("PRAGMA synchronous=NORMAL")
                (version,) = SQLiteUserVersion.read(connection.execute("PRAGMA user_version"))
                # Opening an installed index is a read. Only initialization
                # needs a writer; recheck after taking it because another
                # process may have initialized the file since this read.
                if version.user_version != _INDEX_VERSION:
                    connection.execute("BEGIN IMMEDIATE")
                    (version,) = SQLiteUserVersion.read(connection.execute("PRAGMA user_version"))
                    if version.user_version == 0:
                        if _IndexTable.read(
                            connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                        ):
                            raise ValueError("Reply index requires the quiet runtime reset")
                        for table in TypedTable.members_with(ReplyIndexTable):
                            table.create(connection)
                        connection.execute(f"PRAGMA user_version={_INDEX_VERSION}")
                    elif version.user_version != _INDEX_VERSION:
                        raise ValueError("Reply index requires the quiet runtime reset")
                    connection.commit()
            except (sqlite3.DatabaseError, ValueError):
                connection.close()
                raise
            self._connection = self._database_resources.enter_context(closing(connection))
            self._database_inode = self.index_path.stat().st_ino
        return self._connection

    def _discard_database(self) -> None:
        self._close_database()
        for path in CoordinationStore.files(self.index_path):
            path.unlink(missing_ok=True)

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
            with closing(CoordinationStore.writer(self.index_path)) as database, database:
                for table in TypedTable.members_with(ReplyIndexTable):
                    database.executemany(
                        f"DELETE FROM {table.declared_name} WHERE source = ?",
                        ((source,) for source in sources),
                    )

    @contextmanager
    def _indexed_sources(
        self, revisions: dict[str, FileRevision | None], seen: Mapping[str, int]
    ) -> Iterator[tuple[sqlite3.Connection, dict[str, ReplyIndex]]]:
        database = self._database()
        # Publish before reading committed counts, if the preparation has finished.
        self._preparation = self._preparation.finished(self, database, revisions)
        with database:
            CoordinationStore.begin_snapshot(database)
            indexes = ReplyIndex.for_sources(database, tuple(revisions))
            needed = {
                source: (indexes[source], revision, seen[source])
                for source, revision in revisions.items()
                if revision is not None and not indexes[source].counts(revision, seen[source])
            }
            yield database, indexes
        # Start only after releasing the read snapshot. Spawn receives data and
        # cancellation, never this connection, the observer pipe or the owner's locks.
        self._preparation = self._preparation.started(self, needed)

    def _refresh_keys(self, visibility: RosterVisibility) -> frozenset[str]:
        changed = self._source_watch.changed_paths()
        invalid = self._source_watch.invalid
        snapshot = self.registry.snapshot()
        visible = frozenset(thread.name for thread in snapshot.visible_threads(visibility))
        threads = {executor.name: executor for thread in snapshot.threads.values()
                   for executor in thread.role.executor(thread)}
        rebuild = bool(self._thread_keys.keys() - threads.keys()) or invalid
        for name in self._thread_keys.keys() - threads.keys():
            del self._thread_keys[name]
        for name, thread in threads.items():
            prior = self._thread_keys.get(name)
            if prior is None and name not in visible:
                continue
            if prior is None or not prior.matches(thread):
                current = TranscriptReadKeys.capture(thread, {})
            elif invalid or any(Path(key).absolute() in changed for key in prior.revisions):
                observed = {key: FileRevision.capture(Path(key)) for key in prior.revisions}
                if observed == prior.revisions:
                    continue
                current = TranscriptReadKeys.capture(thread, observed)
            else:
                continue
            rebuild |= prior is None or prior.revisions.keys() != current.revisions.keys()
            self._thread_keys[name] = current
        if rebuild:
            self._source_resources.close()
            sources = tuple(WatchSource.modified_file(Path(key).absolute())
                            for keys in self._thread_keys.values() for key in keys.revisions)
            wanted = {key for keys in self._thread_keys.values() for key in keys.revisions}
            self._preparation.retain(wanted)
            if not sources:
                self._preparation.cancel()
                self._preparation = self._idle
            self._source_watch = self._source_resources.enter_context(closing(
                NativeFileWatch.open(*sources)
            ))
            # Cover a write between the first capture and installing its watch.
            for name, keys in tuple(self._thread_keys.items()):
                observed = {key: FileRevision.capture(Path(key)) for key in keys.revisions}
                if observed != keys.revisions:
                    self._thread_keys[name] = TranscriptReadKeys.capture(threads[name], observed)
        return visible

    def counts(self, viewer: str, *, document: ReadDocument,
               visibility: RosterVisibility) -> TranscriptUnread:
        # Counts never wait for native decoding or another in-process reader.
        if not self._lock.acquire(blocking=False):
            raise CoordinationReadUnavailable("Transcript counts are being read")
        try:
            if not self._cleanup.alive:
                raise RuntimeError("Transcript reader is closed")
            visible = self._refresh_keys(visibility)
            try:
                result = self._preparation.read_counts(self, viewer, document)
            except sqlite3.DatabaseError as error:
                if error.sqlite_errorcode not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                    CoordinationReadUnavailable.raise_from(error)
                self._discard_database()
                result = self._preparation.read_counts(self, viewer, document)
            return TranscriptUnread(
                {name: count for name, count in result.counts.items() if name in visible},
                result.pending & visible,
            )
        finally:
            self._lock.release()

    def _counts_locked(
        self, viewer: str, document: ReadDocument
    ) -> TranscriptUnread:
        revisions = {source: revision for keys in self._thread_keys.values()
                     for source, revision in keys.revisions.items()}
        if not revisions:
            return TranscriptUnread({name: 0 for name in self._thread_keys}, frozenset())
        per_file: dict[str, FileReplyCount] = {}
        seen = {}
        for source, revision in revisions.items():
            if revision is not None:
                mark = self.reads.transcript_seen(
                    viewer, source, revision.identity.inode, document=document
                )
                seen[source] = mark if mark <= revision.size else 0
        with self._indexed_sources(revisions, seen) as (database, indexes):
            ends: dict[str, int] = {}
            for source, revision in revisions.items():
                if revision is None or seen[source] == revision.size:
                    per_file[source] = Counted(0)
                elif not indexes[source].counts(revision, seen[source]):
                    per_file[source] = Indexing()
                else:
                    ends[source] = seen[source]
            for source, count in TranscriptReply.after(database, ends).items():
                per_file[source] = Counted(count)
        result: dict[str, int] = {}
        pending: set[str] = set()
        for name, keys in self._thread_keys.items():
            total = sum((per_file[source] for source in keys.revisions), Counted(0))
            total.record(name, result, pending)
        return TranscriptUnread(result, frozenset(pending))

    def mark_read(self, viewer: str, source: str, through: int, cut: SessionCut) -> None:
        cut.mark_read(self.reads, viewer, source, through)


_shared_lock = RLock()
_shared_states: WeakValueDictionary[str, TranscriptReadState] = WeakValueDictionary()


def transcript_read_state(path: Path, registry: Registration) -> TranscriptReadState:
    """Reuse one source index per live wire root, without retaining dead roots."""
    key = str(path.expanduser().resolve())
    with _shared_lock:
        state = _shared_states.get(key)
        if state is None or not state._cleanup.alive:
            state = TranscriptReadState(Path(key), registry)
            _shared_states[key] = state
        return state
