"""Human read positions for native transcripts, independent of inbox delivery."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, RLock
from time import monotonic
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from .coordination_database import CoordinationStore, StoreBusy
from .native_entries import NativeEntry
from .private_path import FileRevision
from .read_ledger import ReadDocument, ReadLedger
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
        return {row.source: row for row in rows}

    def covers(self, revision: FileRevision) -> bool:
        return self.complete and self.revision == revision


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

    deadline: float
    cancelled: Event
    bytes_left: int = 1024 * 1024
    records_left: int = 512

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
        self._index_path = path.with_name(f"transcript_reply_index.v{_INDEX_VERSION}.sqlite3")
        self._connection: sqlite3.Connection | None = None
        self._database_inode: int | None = None
        self._lock = RLock()
        self._cancelled = Event()
        self._next_source = 0

    def close(self) -> None:
        """Cancel indexing before waiting for its transaction and closing SQLite."""
        self._cancelled.set()
        with self._lock:
            self._close_database()

    def _close_database(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
            self._database_inode = None

    def __del__(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()

    def _database(self) -> sqlite3.Connection:
        revision = FileRevision.capture(self._index_path)
        if self._connection is not None and (
            revision is None or revision.identity.inode != self._database_inode
        ):
            self._connection.close()
            self._connection = None
        if self._connection is None:
            self._index_path.parent.mkdir(parents=True, exist_ok=True)
            connection = CoordinationStore.writer(
                self._index_path, StoreBusy(), check_same_thread=False
            )
            try:
                connection.execute("PRAGMA synchronous=NORMAL")
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
            self._connection = connection
            self._database_inode = self._index_path.stat().st_ino
        return self._connection

    def _discard_database(self) -> None:
        self._close_database()
        for suffix in ("", "-journal", "-wal", "-shm"):
            self._index_path.with_name(self._index_path.name + suffix).unlink(missing_ok=True)

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        """Drop the reply projection of each deleted thread's transcript."""
        sources = tuple(thread.session_file for thread in threads if thread.session_file)
        if not sources or not self._index_path.exists():
            return
        # Deletion waits for the indexer instead of sharing its UI budget.
        with self._lock, closing(CoordinationStore.writer(self._index_path)) as database:
            with database:
                for table in (ReplyIndex, TranscriptReply):
                    database.executemany(
                        f"DELETE FROM {table.declared_name} WHERE source = ?",
                        ((source,) for source in sources),
                    )

    def _index(
        self, source: str, revision: FileRevision, budget: IndexSlice,
        database: sqlite3.Connection, cached: ReplyIndex | None,
    ) -> None:
        if not budget.available:
            return
        resume = (
            cached is not None and cached.revision is not None
            and cached.through <= cached.revision.size
            and cached.revision.holds_cut(revision, cached.revision.size)
        )
        through, total = (cached.through, cached.total) if resume else (0, 0)
        complete = False
        replies: list[TranscriptReply] = []
        # Native decoding has no SQLite transaction or cross-process writer lock.
        with Path(source).open("rb") as stream:
            if not revision.opened_by(stream):
                return
            stream.seek(through)
            for _ in range(256):
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
                record = NativeEntry.read(raw)
                if record.unread_reply:
                    total += 1
                    replies.append(TranscriptReply(source, through, total))
        index = ReplyIndex(revision, through, total, source, complete or through == revision.size)
        with database:
            database.execute("BEGIN IMMEDIATE")
            if ReplyIndex.one(database, source=source) != cached:
                # Another observer's commit wins. The caller rereads one snapshot;
                # never apply these ordinals to a different committed prefix.
                return
            if not resume:
                database.execute(
                    f"DELETE FROM {TranscriptReply.declared_name} WHERE source = ?", (source,)
                )
            for reply in replies:
                reply.insert(database)
            index.upsert(database)

    @contextmanager
    def _indexed_sources(
        self, revisions: dict[str, FileRevision | None], budget: IndexSlice
    ) -> Iterator[tuple[sqlite3.Connection, dict[str, ReplyIndex]]]:
        """Advance missing projections lock-free, then borrow one committed read.

        Ready sources keep their original snapshot: no second row decode. An
        unfinished source releases that read before native IO and reopens it
        after the short compare-and-commit, so totals and predecessors agree.
        """
        database = self._database()
        with database:
            CoordinationStore.begin_snapshot(database)
            indexes = ReplyIndex.for_sources(database, tuple(revisions))
            needed = [
                (source, revision) for source, revision in revisions.items()
                if revision is not None
                and (source not in indexes or not indexes[source].covers(revision))
            ]
            if needed:
                database.rollback()
                start = self._next_source % len(needed)
                scanned = 0
                for offset in range(len(needed)):
                    source, revision = needed[(start + offset) % len(needed)]
                    available = budget.available
                    try:
                        self._index(source, revision, budget, database, indexes.get(source))
                    except FileNotFoundError:
                        revisions[source] = None
                    if available:
                        scanned = offset + 1
                self._next_source = start + max(scanned, 1)
                CoordinationStore.begin_snapshot(database)
                indexes = ReplyIndex.for_sources(database, tuple(revisions))
            yield database, indexes

    def counts(
        self, viewer: str, sources: Mapping[str, str], *, document: ReadDocument | None = None
    ) -> TranscriptUnread:
        # A cancelled UI waiter leaves at most one bounded batch in its executor.
        # Include lock contention in the same budget rather than queueing scans.
        deadline = monotonic() + 0.15
        if self._cancelled.is_set() or not self._lock.acquire(
            timeout=max(0, deadline - monotonic())
        ):
            return TranscriptUnread({}, frozenset(sources))
        try:
            budget = IndexSlice(deadline, self._cancelled)
            document = self.reads.read() if document is None else document
            try:
                return self._counts_locked(viewer, sources, budget, document)
            except sqlite3.DatabaseError as error:
                if error.sqlite_errorcode & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    # Another process is indexing: the same answer as a busy lock here.
                    return TranscriptUnread({}, frozenset(sources))
                if error.sqlite_errorcode not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                    raise
                self._discard_database()
                return self._counts_locked(viewer, sources, budget, document)
        finally:
            self._lock.release()

    def _counts_locked(
        self, viewer: str, sources: Mapping[str, str], budget: IndexSlice, document: ReadDocument
    ) -> TranscriptUnread:
        if not any(sources.values()):
            return TranscriptUnread(dict.fromkeys(sources, 0), frozenset())
        revisions = {
            source: FileRevision.capture(Path(source)) if source else None
            for source in dict.fromkeys(sources.values())
        }
        result: dict[str, int] = {}
        pending: set[str] = set()
        with self._indexed_sources(revisions, budget) as (database, indexes):
            ends: dict[str, int] = {}
            completed: dict[str, ReplyIndex] = {}
            for name, source in sources.items():
                revision = revisions[source]
                index = indexes.get(source)
                if revision is None:
                    result[name] = 0
                elif index is None or not index.covers(revision):
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
        if state is None or state._cancelled.is_set():
            state = TranscriptReadState(Path(key))
            _shared_states[key] = state
        return state
