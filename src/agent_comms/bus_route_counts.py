"""Disposable prefix counts for bounded-cost unread projections.

The JSONL bus owns every route. This index is rebuilt from it when the bus is
replaced, and advances only across complete appended rows under the bus lock.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any


class BusRouteCounts:
    def __init__(self, bus_path: Path):
        self.bus_path = bus_path
        self.path = bus_path.with_name("bus_route_counts.sqlite3")
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS route_prefixes ("
            "id INTEGER PRIMARY KEY, seq INTEGER NOT NULL, target TEXT NOT NULL, "
            "sender TEXT NOT NULL, target_count INTEGER NOT NULL, pair_count INTEGER NOT NULL)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS route_target_seq "
            "ON route_prefixes(target, seq DESC, id DESC)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS route_pair_seq "
            "ON route_prefixes(target, sender, seq DESC, id DESC)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS route_totals ("
            "target TEXT PRIMARY KEY, total INTEGER NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS pair_totals ("
            "target TEXT NOT NULL, sender TEXT NOT NULL, total INTEGER NOT NULL, "
            "PRIMARY KEY(target, sender)) WITHOUT ROWID"
        )
        self._target_after: dict[tuple[str, int], int] = {}
        self._pair_after: dict[tuple[str, str, int], int] = {}

    def __enter__(self) -> BusRouteCounts:
        return self

    def __exit__(self, *_error: object) -> None:
        self.connection.close()

    def sync(self, parse_route: Callable[[Mapping[str, Any]], tuple[int, str, str]]) -> bool:
        """Return false for an incomplete tail; caller then uses the bus directly."""
        try:
            source = self.bus_path.open("rb")
        except FileNotFoundError:
            source = None
        try:
            if source is None:
                identity = None
                size = 0
                tail_digest = None
            else:
                st = os.fstat(source.fileno())
                identity = [st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns]
                size = st.st_size
                if size:
                    source.seek(-1, os.SEEK_END)
                    if source.read(1) != b"\n":
                        return False
                source.seek(max(0, size - 4096))
                tail_digest = sha256(source.read()).hexdigest()
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key = 'source'"
            ).fetchone()
            recorded = json.loads(row[0]) if row else None
            if not isinstance(recorded, dict):
                recorded = {}
            recorded_identity = recorded.get("identity")
            raw_offset = recorded.get("offset")
            recorded_offset: int = raw_offset if type(raw_offset) is int and raw_offset >= 0 else 0
            valid_record = (
                recorded.get("version") == 1
                and isinstance(recorded_identity, (list, type(None)))
                and (recorded_identity is None or len(recorded_identity) == 4)
                and type(raw_offset) is int
                and raw_offset >= 0
            )
            rebuild = (
                not valid_record
                or (recorded_identity[:2] if recorded_identity is not None else None)
                != (identity[:2] if identity is not None else None)
                or size < recorded_offset
                or (
                    size == recorded_offset
                    and identity is not None
                    and recorded_identity is not None
                    and identity[2:] != recorded_identity[2:]
                )
            )
            offset = 0 if rebuild else recorded_offset
            if not rebuild and source is not None and offset:
                source.seek(max(0, offset - 4096))
                previous_tail = source.read(offset - max(0, offset - 4096))
                if sha256(previous_tail).hexdigest() != recorded.get("tail_digest"):
                    rebuild = True
                    offset = 0
            if not rebuild and offset == size:
                return True
            target_totals: dict[str, int] = {}
            pair_totals: dict[tuple[str, str], int] = {}
            with self.connection:
                if rebuild:
                    self.connection.execute("DELETE FROM route_prefixes")
                    self.connection.execute("DELETE FROM route_totals")
                    self.connection.execute("DELETE FROM pair_totals")
                if source is not None:
                    source.seek(offset)
                    for raw in source:
                        if not raw.strip():
                            continue
                        record = json.loads(raw)
                        if not isinstance(record, Mapping):
                            raise ValueError("JSONL bus row must be an object.")
                        seq, sender, target = parse_route(record)
                        if target not in target_totals:
                            prior = self.connection.execute(
                                "SELECT total FROM route_totals WHERE target = ?", (target,)
                            ).fetchone()
                            target_totals[target] = prior[0] if prior else 0
                        pair = target, sender
                        if pair not in pair_totals:
                            prior = self.connection.execute(
                                "SELECT total FROM pair_totals WHERE target = ? AND sender = ?",
                                pair,
                            ).fetchone()
                            pair_totals[pair] = prior[0] if prior else 0
                        target_totals[target] += 1
                        pair_totals[pair] += 1
                        self.connection.execute(
                            "INSERT INTO route_prefixes "
                            "(seq, target, sender, target_count, pair_count) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (seq, target, sender, target_totals[target], pair_totals[pair]),
                        )
                self.connection.executemany(
                    "INSERT OR REPLACE INTO route_totals VALUES (?, ?)", target_totals.items()
                )
                self.connection.executemany(
                    "INSERT OR REPLACE INTO pair_totals VALUES (?, ?, ?)",
                    ((target, sender, total) for (target, sender), total in pair_totals.items()),
                )
                self.connection.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('source', ?)",
                    (
                        json.dumps(
                            {
                                "version": 1,
                                "identity": identity,
                                "offset": size,
                                "tail_digest": tail_digest,
                            }
                        ),
                    ),
                )
            return True
        finally:
            if source is not None:
                source.close()

    def senders(self, target: str) -> tuple[str, ...]:
        return tuple(
            row[0]
            for row in self.connection.execute(
                "SELECT sender FROM pair_totals WHERE target = ?", (target,)
            )
        )

    def routes(self) -> tuple[tuple[str, str], ...]:
        return tuple(self.connection.execute("SELECT target, sender FROM pair_totals"))

    def target_after(self, target: str, through: int) -> int:
        key = target, through
        if key not in self._target_after:
            total = self.connection.execute(
                "SELECT total FROM route_totals WHERE target = ?", (target,)
            ).fetchone()
            prefix = self.connection.execute(
                "SELECT target_count FROM route_prefixes "
                "WHERE target = ? AND seq <= ? ORDER BY seq DESC, id DESC LIMIT 1",
                key,
            ).fetchone()
            self._target_after[key] = (total[0] if total else 0) - (prefix[0] if prefix else 0)
        return self._target_after[key]

    def pair_after(self, target: str, sender: str, through: int) -> int:
        key = target, sender, through
        if key not in self._pair_after:
            total = self.connection.execute(
                "SELECT total FROM pair_totals WHERE target = ? AND sender = ?",
                (target, sender),
            ).fetchone()
            prefix = self.connection.execute(
                "SELECT pair_count FROM route_prefixes "
                "WHERE target = ? AND sender = ? AND seq <= ? "
                "ORDER BY seq DESC, id DESC LIMIT 1",
                key,
            ).fetchone()
            self._pair_after[key] = (total[0] if total else 0) - (prefix[0] if prefix else 0)
        return self._pair_after[key]
