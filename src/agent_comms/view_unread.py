"""Human read positions for native transcripts, independent of inbox delivery."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from weakref import WeakValueDictionary

from .declarations import _atomic_write_text, _store_lock, file_revision

_INDEX_VERSION = 1


@dataclass(frozen=True)
class ReplyIndex:
    revision: tuple[int, int, int, int] | None = None
    through: int = 0
    total: int = 0


class TranscriptReadState:
    """Index completed replies incrementally; keep the human cursor separate.

    The SQLite index is a disposable projection of the native session files. It
    stores reply end offsets and ordinals, so a new process can count unread
    replies with an indexed predecessor lookup instead of parsing all history.
    Source inode and revision invalidate an old projection after replacement.
    """

    def __init__(self, path: Path):
        self.path = path
        self._index_path = path.with_name("transcript_reply_index.sqlite3")
        self._connection: sqlite3.Connection | None = None
        self._database_inode: int | None = None
        self._lock = RLock()

    def close(self) -> None:
        """Release the process-local database handle; the index remains durable."""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
                self._database_inode = None

    def __del__(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()

    def _database(self) -> sqlite3.Connection:
        revision = file_revision(self._index_path)
        if self._connection is not None and (
            revision is None or revision[0] != self._database_inode
        ):
            self._connection.close()
            self._connection = None
        if self._connection is None:
            self._index_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._index_path, check_same_thread=False)
            try:
                connection.execute("PRAGMA synchronous=NORMAL")
                # A future reply-classification change increments this version;
                # an old derived projection must then be rebuilt from the file.
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version != _INDEX_VERSION:
                    connection.execute("DROP TABLE IF EXISTS replies")
                    connection.execute("DROP TABLE IF EXISTS sources")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS sources ("
                    "source TEXT PRIMARY KEY, inode INTEGER NOT NULL, size INTEGER NOT NULL, "
                    "mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL, "
                    "through INTEGER NOT NULL, total INTEGER NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS replies ("
                    "source TEXT NOT NULL, end INTEGER NOT NULL, ordinal INTEGER NOT NULL, "
                    "PRIMARY KEY(source, end)) WITHOUT ROWID"
                )
                connection.execute(f"PRAGMA user_version={_INDEX_VERSION}")
                connection.commit()
            except sqlite3.DatabaseError:
                connection.close()
                raise
            self._connection = connection
            self._database_inode = self._index_path.stat().st_ino
        return self._connection

    def _discard_database(self) -> None:
        self.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            self._index_path.with_name(self._index_path.name + suffix).unlink(missing_ok=True)

    def _index(self, source: str, is_reply: Callable[[Mapping], bool]) -> ReplyIndex:
        path = Path(source)
        revision = file_revision(path) if source else None
        if revision is None:
            return ReplyIndex()
        database = self._database()
        row = database.execute(
            "SELECT inode, size, mtime_ns, ctime_ns, through, total "
            "FROM sources WHERE source = ?",
            (source,),
        ).fetchone()
        cached = ReplyIndex(tuple(row[:4]), row[4], row[5]) if row else ReplyIndex()
        if cached.revision == revision and cached.through <= revision[1]:
            return cached
        append = (
            cached.revision is not None
            and cached.revision[0] == revision[0]
            and revision[1] > cached.revision[1]
            and cached.through <= cached.revision[1]
        )
        through, total = (cached.through, cached.total) if append else (0, 0)
        with database:
            if not append:
                database.execute("DELETE FROM replies WHERE source = ?", (source,))
            with path.open("rb") as stream:
                stream.seek(through)
                while stream.tell() < revision[1]:
                    raw = stream.readline(revision[1] - stream.tell())
                    if not raw.endswith(b"\n"):
                        break  # A writer's incomplete trailing record is not a reply.
                    through = stream.tell()
                    try:
                        record = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(record, dict) and is_reply(record):
                        total += 1
                        database.execute(
                            "INSERT INTO replies(source, end, ordinal) VALUES (?, ?, ?)",
                            (source, through, total),
                        )
            database.execute(
                "INSERT OR REPLACE INTO sources "
                "(source, inode, size, mtime_ns, ctime_ns, through, total) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source, *revision, through, total),
            )
        return ReplyIndex(revision, through, total)

    @staticmethod
    def _key(viewer: str, source: str, inode: int) -> str:
        return json.dumps([viewer, str(Path(source).resolve()), inode])

    def _markers(self) -> dict[str, int]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def counts(
        self, viewer: str, sources: Mapping[str, str], is_reply: Callable[[Mapping], bool]
    ) -> dict[str, int]:
        with self._lock, _store_lock(self.path):
            try:
                return self._counts_locked(viewer, sources, is_reply)
            except sqlite3.DatabaseError:
                # The index is disposable. A damaged cache cannot make unread
                # state unavailable; rebuild from the authoritative transcript.
                self._discard_database()
                return self._counts_locked(viewer, sources, is_reply)

    def _counts_locked(
        self, viewer: str, sources: Mapping[str, str], is_reply: Callable[[Mapping], bool]
    ) -> dict[str, int]:
        markers = self._markers()
        result = {}
        for name, source in sources.items():
            try:
                index = self._index(source, is_reply)
            except FileNotFoundError:
                result[name] = 0
                continue
            if index.revision is None:
                result[name] = 0
                continue
            seen = markers.get(self._key(viewer, source, index.revision[0]), 0)
            if seen > index.through:
                seen = 0  # A truncated/rebuilt source is a new conversation tail.
            row = (
                self._database()
                .execute(
                    "SELECT ordinal FROM replies WHERE source = ? AND end <= ? "
                    "ORDER BY end DESC LIMIT 1",
                    (source, seen),
                )
                .fetchone()
            )
            result[name] = index.total - (row[0] if row else 0)
        return result

    def mark_read(self, viewer: str, source: str, through: int) -> None:
        revision = file_revision(Path(source)) if source else None
        if revision is None or not 0 <= through <= revision[1]:
            raise ValueError("Transcript changed; refresh before marking it read.")
        with _store_lock(self.path):
            markers = self._markers()
            key = self._key(viewer, source, revision[0])
            previous = markers.get(key, 0)
            if previous > revision[1] or previous < through:
                markers[key] = through
                _atomic_write_text(self.path, json.dumps(markers))


_shared_lock = RLock()
_shared_states: WeakValueDictionary[str, TranscriptReadState] = WeakValueDictionary()


def transcript_read_state(path: Path) -> TranscriptReadState:
    """Reuse one source index per live wire root, without retaining dead roots."""
    key = str(path.expanduser().resolve())
    with _shared_lock:
        state = _shared_states.get(key)
        if state is None:
            state = TranscriptReadState(Path(key))
            _shared_states[key] = state
        return state
