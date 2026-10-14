"""Human read positions for native transcripts, independent of inbox delivery."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, RLock
from time import monotonic
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from .native_entries import NativeEntry
from .private_path import FileRevision
from .read_ledger import ReadLedger
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


@dataclass(frozen=True)
class TranscriptReply(ReplyIndexTable, TypedTable):
    source: str = field(metadata={"sql": Column(primary_key=True)})
    end: int = field(metadata={"sql": Column(primary_key=True)})
    ordinal: int
    without_rowid = True


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
            connection = sqlite3.connect(self._index_path, check_same_thread=False, timeout=0.05)
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
        with self._lock, closing(sqlite3.connect(self._index_path, timeout=5.0)) as database:
            with database:
                for table in (ReplyIndex, TranscriptReply):
                    database.executemany(
                        f"DELETE FROM {table.declared_name} WHERE source = ?",
                        ((source,) for source in sources),
                    )

    def _index(self, source: str, budget: IndexSlice) -> ReplyIndex:
        path = Path(source)
        revision = FileRevision.capture(path) if source else None
        if revision is None:
            return ReplyIndex(complete=True)
        database = self._database()
        if (cached := ReplyIndex.one(database, source=source)) is not None:
            if cached.revision == revision and cached.complete:
                return cached
        if not budget.available:
            return ReplyIndex(revision, source=source)
        complete = False
        with database:
            # Every process showing this bus writes this file. What is indexed
            # is read under the write lock, so no two of them index one record.
            database.execute("BEGIN IMMEDIATE")
            cached = ReplyIndex.one(database, source=source) or ReplyIndex()
            unchanged = cached.revision == revision
            if unchanged and cached.complete:
                return cached
            append = (
                cached.revision is not None
                and cached.revision.identity == revision.identity
                and revision.size > cached.revision.size
                and cached.through <= cached.revision.size
            )
            resume = unchanged or append
            through, total = (cached.through, cached.total) if resume else (0, 0)
            if not resume:
                database.execute(
                    f"DELETE FROM {TranscriptReply.declared_name} WHERE source = ?", (source,)
                )
            with path.open("rb") as stream:
                stream.seek(through)
                # Give other sources a turn even when this session is enormous.
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
                    # A complete native line that does not decode is a damaged
                    # session, not an unread count of zero for that line.
                    record = NativeEntry.read(raw)
                    if record.unread_reply:
                        total += 1
                        TranscriptReply(source, through, total).insert(database)
            complete = complete or through == revision.size
            index = ReplyIndex(revision, through, total, source, complete)
            index.upsert(database)
        return index

    def counts(self, viewer: str, sources: Mapping[str, str]) -> TranscriptUnread:
        # A cancelled UI waiter leaves at most one bounded batch in its executor.
        # Include lock contention in the same budget rather than queueing scans.
        deadline = monotonic() + 0.15
        if self._cancelled.is_set() or not self._lock.acquire(timeout=0.15):
            return TranscriptUnread({}, frozenset(sources))
        try:
            budget = IndexSlice(deadline, self._cancelled)
            try:
                return self._counts_locked(viewer, sources, budget)
            except sqlite3.DatabaseError as error:
                if error.sqlite_errorcode & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    # Another process is indexing: the same answer as a busy lock here.
                    return TranscriptUnread({}, frozenset(sources))
                if error.sqlite_errorcode not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                    raise
                self._discard_database()
                return self._counts_locked(viewer, sources, budget)
        finally:
            self._lock.release()

    def _counts_locked(
        self, viewer: str, sources: Mapping[str, str], budget: IndexSlice
    ) -> TranscriptUnread:
        result: dict[str, int] = {}
        pending: set[str] = set()
        document = self.reads.read()
        items = tuple(sources.items())
        start = self._next_source % len(items) if items else 0
        scanned = 0
        for offset in range(len(items)):
            position = (start + offset) % len(items)
            name, source = items[position]
            available = budget.available
            try:
                index = self._index(source, budget)
            except FileNotFoundError:
                result[name] = 0
                continue
            if available:
                scanned = offset + 1
            if not index.complete:
                pending.add(name)
                continue
            if index.revision is None:
                result[name] = 0
                continue
            seen = self.reads.transcript_seen(
                viewer, source, index.revision.identity.inode, document=document
            )
            if seen > index.revision.size:
                seen = 0  # Only truncation invalidates a cursor beyond the indexed prefix.
            replies = TranscriptReply.read(
                self._database().execute(
                    f"SELECT * FROM {TranscriptReply.declared_name} WHERE source=? AND end<=? "
                    "ORDER BY end DESC LIMIT 1",
                    (source, seen),
                )
            )
            last = next(iter(replies), None)
            result[name] = index.total - (last.ordinal if last is not None else 0)
        self._next_source = start + max(scanned, 1)
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
