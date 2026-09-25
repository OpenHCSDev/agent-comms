"""Disposable byte offsets for bounded JSONL history pages.

The bus remains authoritative. This index is advanced under its file lock and
is never used across a changed inode, shortened file, or changed indexed tail.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO


class StaleBusPageIndex(ValueError):  # noqa: N818 - public index invalidation outcome
    """A disposable offset no longer identifies its claimed wire row."""


class BusPageIndex:
    def __init__(self, bus_path: Path):
        self.bus_path = bus_path
        self.path = bus_path.with_name("bus_page_index.sqlite3")
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS rows ("
            "id INTEGER PRIMARY KEY, seq INTEGER NOT NULL, offset INTEGER NOT NULL, "
            "sender TEXT NOT NULL, target TEXT NOT NULL)"
        )
        self.connection.execute("CREATE INDEX IF NOT EXISTS rows_seq ON rows(seq, id)")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS rows_target_seq ON rows(target, seq, id)"
        )

    def __enter__(self) -> BusPageIndex:
        return self

    def __exit__(self, *_error: object) -> None:
        self.connection.close()

    @staticmethod
    def _tail(stream: BinaryIO, offset: int) -> str:
        start = max(0, offset - 4096)
        stream.seek(start)
        return sha256(stream.read(offset - start)).hexdigest()

    def sync(self) -> bool:
        """Return false for an unfinished tail, which the caller scans normally."""
        try:
            stream = self.bus_path.open("rb")
        except FileNotFoundError:
            return False
        with stream:
            stat = os.fstat(stream.fileno())
            size = stat.st_size
            if size:
                stream.seek(size - 1)
                if stream.read(1) != b"\n":
                    return False
            identity = [stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns]
            saved_row = self.connection.execute(
                "SELECT value FROM metadata WHERE key='source'"
            ).fetchone()
            try:
                saved = json.loads(saved_row[0]) if saved_row else None
            except (TypeError, ValueError):
                saved = None
            offset = saved.get("offset") if isinstance(saved, dict) else None
            previous = saved.get("identity") if isinstance(saved, dict) else None
            valid = (
                isinstance(saved, dict)
                and saved.get("version") == 1
                and type(offset) is int
                and 0 <= offset <= size
                and isinstance(previous, list)
                and len(previous) == 4
                and previous[:2] == identity[:2]
                and (offset != size or previous[2:] == identity[2:])
                and saved.get("tail") == self._tail(stream, offset)
            )
            if not valid:
                offset = 0
            if valid and offset == size:
                return True
            assert isinstance(offset, int)
            with self.connection:
                if not valid:
                    self.connection.execute("DELETE FROM rows")
                    last_sequence = None
                else:
                    last_row = self.connection.execute(
                        "SELECT seq FROM rows ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    last_sequence = last_row[0] if last_row else None
                stream.seek(offset)
                while stream.tell() < size:
                    row_offset = stream.tell()
                    raw = stream.readline()
                    if not raw.endswith(b"\n"):
                        return False
                    if not raw.strip():
                        continue
                    record = json.loads(raw)
                    if not isinstance(record, Mapping):
                        raise ValueError("JSONL bus row must be an object.")
                    # Full validation on the newly indexed segment. A warm
                    # read validates each selected record again from the bus.
                    from .declarations import Message

                    message = Message.from_wire(record)
                    if last_sequence is not None and message.seq <= last_sequence:
                        # The page collector uses wire order. A cache sorted
                        # by sequence must not hide malformed legacy order.
                        raise StaleBusPageIndex("Wire sequences are not increasing.")
                    self.connection.execute(
                        "INSERT INTO rows(seq,offset,sender,target) VALUES(?,?,?,?)",
                        (message.seq, row_offset, message.sender, message.target),
                    )
                    last_sequence = message.seq
                self.connection.execute(
                    "INSERT OR REPLACE INTO metadata VALUES('source',?)",
                    (
                        json.dumps(
                            {
                                "version": 1,
                                "identity": identity,
                                "offset": size,
                                "tail": self._tail(stream, size),
                            }
                        ),
                    ),
                )
            return True

    def offsets(
        self,
        *,
        lower: int | None,
        upper: int | None,
        descending: bool,
        targets: frozenset[str] | None,
    ) -> sqlite3.Cursor:
        clauses: list[str] = []
        params: list[object] = []
        if lower is not None:
            clauses.append("seq > ?")
            params.append(lower)
        if upper is not None:
            clauses.append("seq < ?")
            params.append(upper)
        if targets is not None:
            if not targets:
                return self.connection.execute("SELECT seq,offset,sender,target FROM rows WHERE 0")
            clauses.append("target IN (" + ",".join("?" for _ in targets) + ")")
            params.extend(sorted(targets))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        direction = "DESC" if descending else "ASC"
        query = (
            "SELECT seq,offset,sender,target FROM rows"
            + where
            + f" ORDER BY seq {direction}, id {direction}"
        )
        return self.connection.execute(query, params)

    @staticmethod
    def record(stream: BinaryIO, row: tuple[int, int, str, str]) -> tuple[Mapping, int]:
        seq, offset, sender, target = row
        stream.seek(offset)
        raw = stream.readline()
        try:
            record = json.loads(raw)
        except ValueError as error:
            raise StaleBusPageIndex("Indexed bus row is invalid.") from error
        if (
            not isinstance(record, Mapping)
            or int(record.get("seq", 0)) != seq
            or record.get("from") != sender
            or record.get("to") != target
        ):
            raise StaleBusPageIndex("Indexed bus row changed.")
        return record, len(raw)
