"""Human read positions for native transcripts, independent of inbox delivery."""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
from collections.abc import Collection, Iterator, Mapping, Sequence
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
from .native_entries import NativeEntry
from .private_path import FileRevision
from .read_ledger import ReadDocument, ReadLedger
from .store_files import _store_lock
from .thread_owned_state import ThreadOwnedState
from .typed_table import Column, SQLiteUserVersion, TypedRow, TypedTable

if TYPE_CHECKING:
    from .threads import Thread

_INDEX_VERSION = 4


class ReplyIndexTable:
    """Tables belonging to this disposable transcript reply index."""


@dataclass(frozen=True)
class ReplyIndex(ReplyIndexTable, TypedTable):
    revision: FileRevision | None = None
    through: int = 0
    total: int = 0
    source: str = field(default="", metadata={"sql": Column(primary_key=True, check="source<>''")})
    complete: bool = False

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

    def covers(self, revision: FileRevision) -> bool:
        return self.complete and self.revision == revision

    def continues(self, revision: FileRevision) -> bool:
        return (
            self.revision is not None and self.through <= self.revision.size
            and self.revision.holds_cut(revision, self.revision.size)
        )

    def prepare(self, revision: FileRevision, budget: IndexSlice) -> ReplyIndexUpdate | None:
        """Decode a captured native cut only; this worker never opens SQLite."""
        if not budget.available:
            return None
        resume = self.continues(revision)
        through, total = (self.through, self.total) if resume else (0, 0)
        complete = False
        replies: list[TranscriptReply] = []
        with Path(self.source).open("rb") as stream:
            if not revision.opened_by(stream):
                return None
            stream.seek(through)
            for _ in range(budget.source_records):
                if through == revision.size:
                    complete = True
                    break
                if not budget.available:
                    break
                raw = stream.readline(revision.size - through)
                budget.consumed(len(raw))
                if not raw.endswith(b"\n"):
                    complete = True  # Retry the writer's tail only after a revision change.
                    break
                through = stream.tell()
                if NativeEntry.read(raw).unread_reply:
                    total += 1
                    replies.append(TranscriptReply(self.source, through, total))
        index = ReplyIndex(
            revision, through, total, self.source, complete or through == revision.size
        )
        return ReplyIndexUpdate(self, index, tuple(replies))


@dataclass(frozen=True)
class TranscriptReply(ReplyIndexTable, TypedTable):
    source: str = field(metadata={"sql": Column(primary_key=True)})
    end: int = field(metadata={"sql": Column(primary_key=True)})
    ordinal: int
    without_rowid = True

    @classmethod
    def preceding(
        cls, database: sqlite3.Connection, ends: Mapping[str, int]
    ) -> dict[str, TranscriptReply]:
        """One indexed predecessor per source, from the caller's SQLite snapshot."""
        rows = cls.read(database.execute(
            f'''SELECT reply.* FROM json_each(?) AS displayed
            JOIN {cls.declared_name} AS reply
                ON reply.source=displayed.key AND reply.end=(
                    SELECT max(end) FROM {cls.declared_name}
                    WHERE source=displayed.key AND end<=displayed.value
                )''',
            (json.dumps(ends),),
        ))
        return {row.source: row for row in rows}


@dataclass(frozen=True)
class _IndexTable(TypedRow):
    name: str


@dataclass(frozen=True)
class TranscriptUnread:
    """Counts for completed indexes only; pending names have no exact count yet."""

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
        os.environ.pop("AGENT_COMMS_THREAD", None)
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


@dataclass(frozen=True)
class ReplyIndexUpdate:
    """A native-only prepared delta, admitted against one stored prefix."""

    before: ReplyIndex
    after: ReplyIndex
    replies: tuple[TranscriptReply, ...]

    def commit(self, database: sqlite3.Connection) -> None:
        current = ReplyIndex.one(database, source=self.after.source) or ReplyIndex(
            source=self.after.source
        )
        if current != self.before:
            return  # Another observer's prefix won; never apply these ordinals to it.
        if not self.before.continues(self.after.revision):
            database.execute(
                f"DELETE FROM {TranscriptReply.declared_name} WHERE source=?", (self.after.source,)
            )
        for reply in self.replies:
            reply.insert(database)
        self.after.upsert(database)


@dataclass(frozen=True)
class ReplyIndexBatch:
    """The compact result of one bounded, fair native-file preparation unit."""

    updates: tuple[ReplyIndexUpdate, ...]
    last_source: str | None

    @classmethod
    def prepare(cls, reads: tuple[tuple[ReplyIndex, FileRevision], ...]) -> ReplyIndexBatch:
        budget = IndexSlice.worker_slice()
        updates = []
        last_source = None
        for index, revision in reads:
            if not budget.available:
                break
            last_source = index.source
            try:
                update = index.prepare(revision, budget)
            except FileNotFoundError:
                continue  # A later source observation sees the missing file.
            if update is not None:
                updates.append(update)
        return cls(tuple(updates), last_source)


@dataclass
class NativeReplyPreparation:
    """The actual admitted work and future; deletion withdraws its original reads."""

    reads: dict[str, tuple[ReplyIndex, FileRevision]]
    future: Future[ReplyIndexBatch]

    def retire(self, sources: Collection[str]) -> None:
        for source in sources:
            self.reads.pop(source, None)
        if not self.reads:
            self.future.cancel()

    def commit(self, database: sqlite3.Connection, wanted: Collection[str]) -> str | None:
        if self.future.cancelled():
            return None
        batch = self.future.result()
        try:
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
        except sqlite3.OperationalError as error:
            CoordinationReadUnavailable.raise_from(error)
        return batch.last_source


class TranscriptReadState(ThreadOwnedState):
    """Index completed replies incrementally; keep the human cursor separate.

    The SQLite index is a disposable projection of the native session files. It
    stores reply end offsets and ordinals, so a new process can count unread
    replies with an indexed predecessor lookup instead of parsing all history.
    Source inode and revision invalidate an old projection after replacement.
    """

    def __init__(self, path: Path):
        self.path = path.with_name(ReadLedger.filename)
        self.reads = ReadLedger(self.path)
        self.index_path = path.with_name(f"transcript_reply_index.v{_INDEX_VERSION}.sqlite3")
        self._connection: sqlite3.Connection | None = None
        self._database_inode: int | None = None
        self._lock = RLock()
        self._context = multiprocessing.get_context("spawn")
        self._cancelled = self._context.Event()
        self._next_source: str | None = None
        self._pool: ProcessPoolExecutor | None = None
        self._preparation: NativeReplyPreparation | None = None
        self._database_resources = ExitStack()
        self._resources = ExitStack()
        self._resources.callback(self._database_resources.close)
        # Capture the resource stack, not this state: dead roots must remain collectible.
        self._cleanup = finalize(self, self._resources.close)

    def close(self) -> None:
        """Retire the worker and SQLite through their shared resource lifetime."""
        with self._lock:
            if self._preparation is not None:
                self._preparation.future.cancel()
            self._cleanup()
            self._pool = None
            self._preparation = None
            self._close_database()

    @property
    def preparation(self) -> Future[ReplyIndexBatch] | None:
        """The admitted completion itself, for the observer's existing wakeup."""
        return self._preparation.future if self._preparation is not None else None

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
        for suffix in ("", "-journal", "-wal", "-shm"):
            self.index_path.with_name(self.index_path.name + suffix).unlink(missing_ok=True)

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        """Drop the reply projection of each deleted thread's transcript."""
        sources = tuple(thread.session_file for thread in threads if thread.session_file)
        if not sources:
            return
        with self._lock:
            if self._preparation is not None:
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
        self, revisions: dict[str, FileRevision | None]
    ) -> Iterator[tuple[sqlite3.Connection, dict[str, ReplyIndex]]]:
        database = self._database()
        preparation = self._preparation
        if preparation is not None and preparation.future.done():
            # Publishing prepared work is independent of reading committed counts.
            # A busy writer retains this exact work; it does not invalidate other
            # sources. The wire lock still orders publication against deletion.
            try:
                with _store_lock(self.path.parent / "wire", shared=True, blocking=False):
                    self._next_source = preparation.commit(database, revisions)
            except (BlockingIOError, CoordinationReadUnavailable):
                pass
            else:
                self._preparation = None
        with database:
            CoordinationStore.begin_snapshot(database)
            indexes = ReplyIndex.for_sources(database, tuple(revisions))
            needed = {
                source: (indexes[source], revision)
                for source, revision in revisions.items()
                if revision is not None and not indexes[source].covers(revision)
            }
            yield database, indexes
        # Admit only after releasing the read snapshot. Spawn receives data and
        # cancellation, never this connection, the observer pipe or owner custody.
        if needed and self._preparation is None and self._cleanup.alive:
            names = tuple(needed)
            start = names.index(self._next_source) + 1 if self._next_source in names else 0
            reads = tuple(needed.values())
            ordered = (*reads[start:], *reads[:start])
            if self._pool is None:
                self._pool = ProcessPoolExecutor(
                    max_workers=1, mp_context=self._context,
                    initializer=IndexSlice.initialize_worker, initargs=(self._cancelled,),
                )
                self._resources.callback(self._pool.shutdown, wait=True, cancel_futures=True)
                self._resources.callback(self._cancelled.set)
            self._preparation = NativeReplyPreparation(
                needed, self._pool.submit(ReplyIndexBatch.prepare, ordered)
            )

    def counts(
        self, viewer: str, sources: Mapping[str, str], *, document: ReadDocument | None = None
    ) -> TranscriptUnread:
        # Counts never wait for native decoding or another in-process reader.
        if not self._lock.acquire(blocking=False):
            raise CoordinationReadUnavailable("Transcript counts are being read")
        try:
            if not self._cleanup.alive:
                raise RuntimeError("Transcript reader is closed")
            document = self.reads.read() if document is None else document
            try:
                return self._counts_locked(viewer, sources, document)
            except sqlite3.DatabaseError as error:
                if error.sqlite_errorcode not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                    CoordinationReadUnavailable.raise_from(error)
                self._discard_database()
                return self._counts_locked(viewer, sources, document)
        finally:
            self._lock.release()

    def _counts_locked(
        self, viewer: str, sources: Mapping[str, str], document: ReadDocument
    ) -> TranscriptUnread:
        if not any(sources.values()) and self._preparation is None:
            return TranscriptUnread(dict.fromkeys(sources, 0), frozenset())
        revisions = {
            source: FileRevision.capture(Path(source)) if source else None
            for source in dict.fromkeys(sources.values())
        }
        result: dict[str, int] = {}
        pending: set[str] = set()
        with self._indexed_sources(revisions) as (database, indexes):
            ends: dict[str, int] = {}
            completed: dict[str, ReplyIndex] = {}
            for name, source in sources.items():
                revision = revisions[source]
                index = indexes[source]
                if revision is None:
                    result[name] = 0
                elif not index.covers(revision):
                    pending.add(name)
                else:
                    completed[name] = index
                    seen = self.reads.transcript_seen(
                        viewer, source, revision.identity.inode, document=document
                    )
                    if seen > revision.size:
                        seen = 0  # Only truncation invalidates a cursor beyond the indexed prefix.
                    ends[source] = seen
            replies = TranscriptReply.preceding(database, ends)
            for name, index in completed.items():
                last = replies.get(index.source)
                result[name] = index.total - (last.ordinal if last is not None else 0)
        return TranscriptUnread(result, frozenset(pending))

    def mark_read(self, viewer: str, source: str, through: int) -> None:
        revision = FileRevision.capture(Path(source)) if source else None
        if revision is None or not 0 <= through <= revision.size:
            raise ValueError("Transcript changed; refresh before marking it read.")
        self.reads.mark_transcript(
            viewer, source, revision.identity.inode, through, revision.size
        )


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
