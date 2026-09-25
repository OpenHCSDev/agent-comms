"""Durable goal revision history alongside the rolling-compatible registry.

The registry Goal remains the current-state authority. A history intent is
synced before that registry changes; only a synced commit, or a later guarded
reconciliation against the registry, makes the transition readable. Older
registry writers can replace registry.json without erasing this journal.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .declarations import Goal


class GoalHistoryError(RuntimeError):
    """Goal history could not be durably written or reconciled."""


@dataclass(frozen=True, slots=True)
class GoalHistoryEntry:
    sequence: int
    kind: Literal["transition", "baseline", "observed_gap"]
    observed_at: float
    before: Goal | None
    after: Goal | None


class GoalHistoryStore:
    """One journal per registry root; callers hold the registry file lock."""

    def __init__(self, registry_path: Path):
        self.registry_path = registry_path
        self.path = registry_path.parent / "goal_history.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=EXTRA")
            return connection
        except sqlite3.Error as error:
            raise GoalHistoryError("Goal history database is unavailable.") from error

    def _sync(self) -> None:
        self._sync_file(self.path)
        if os.name == "posix":
            self._sync_directory(self.path.parent)

    @staticmethod
    def _sync_file(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _initialize(self) -> None:
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        except OSError as error:
            raise GoalHistoryError("Cannot create goal history database.") from error
        else:
            os.close(descriptor)
        if os.name == "posix" and stat.S_IMODE(self.path.stat().st_mode) != 0o600:
            raise GoalHistoryError("Goal history database must be owner-only.")
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS metadata "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS entries ("
                    "sequence INTEGER PRIMARY KEY, owner_created_at REAL NOT NULL, "
                    "kind TEXT NOT NULL CHECK(kind IN ('transition','baseline','observed_gap')), "
                    "state TEXT NOT NULL "
                    "CHECK(state IN ('pending','committed','aborted','uncertain')), "
                    "observed_at REAL NOT NULL, before_goal TEXT, after_goal TEXT)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS entries_owner_sequence "
                    "ON entries(owner_created_at,sequence)"
                )
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
                if version is None:
                    connection.execute(
                        "INSERT INTO metadata(key,value) VALUES('schema_version','1')"
                    )
                elif version != ("1",):
                    raise GoalHistoryError("Unsupported goal history schema.")
                connection.commit()
            self._sync()
        except (OSError, sqlite3.Error) as error:
            raise GoalHistoryError("Goal history initialization is uncertain.") from error

    @staticmethod
    def _encode(goal: Goal | None) -> str | None:
        return json.dumps(asdict(goal), sort_keys=True) if goal is not None else None

    @staticmethod
    def _decode(raw: str | None) -> Goal | None:
        try:
            value = json.loads(raw) if raw is not None else None
            return Goal(**value) if value is not None else None
        except (TypeError, ValueError) as error:
            raise GoalHistoryError("Goal history contains an invalid goal snapshot.") from error

    def _write(self, sql: str, parameters: tuple[object, ...]) -> int:
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(sql, parameters)
                sequence = cursor.lastrowid
                if sequence is None:
                    raise GoalHistoryError("Goal history write returned no row identity.")
                connection.commit()
            self._sync()
            return sequence
        except (OSError, sqlite3.Error) as error:
            raise GoalHistoryError("Goal history write durability is uncertain.") from error

    def _insert(
        self,
        owner_created_at: float,
        kind: Literal["transition", "baseline", "observed_gap"],
        state: Literal["pending", "committed"],
        before: Goal | None,
        after: Goal | None,
    ) -> int:
        return self._write(
            "INSERT INTO entries(owner_created_at,kind,state,observed_at,before_goal,after_goal) "
            "VALUES(?,?,?,?,?,?)",
            (
                owner_created_at,
                kind,
                state,
                time.time(),
                self._encode(before),
                self._encode(after),
            ),
        )

    def _set_state(
        self, sequence: int, state: Literal["committed", "aborted", "uncertain"]
    ) -> None:
        self._write(
            "UPDATE entries SET state=? WHERE sequence=? AND state='pending'", (state, sequence)
        )

    def _sync_visible_registry(self) -> None:
        """Adopt a visible post-crash registry value before attesting an intent."""
        targets = [self.registry_path]
        guard = self.registry_path.parent / ".registry-owner-guard"
        if guard.exists():
            targets.append(guard)
        try:
            for target in targets:
                self._sync_file(target)
            if os.name == "posix":
                self._sync_directory(self.registry_path.parent)
        except OSError as error:
            raise GoalHistoryError("Cannot sync visible goal authority.") from error

    def _rows(self, owner_created_at: float) -> list[tuple]:
        try:
            with closing(self._connect()) as connection:
                return connection.execute(
                    "SELECT sequence,kind,state,observed_at,before_goal,after_goal "
                    "FROM entries WHERE owner_created_at=? ORDER BY sequence",
                    (owner_created_at,),
                ).fetchall()
        except sqlite3.Error as error:
            raise GoalHistoryError("Cannot read goal history.") from error

    def observe(self, owner_created_at: float, current: Goal | None) -> None:
        """Reconcile uncertain writes and label any old-writer gap truthfully."""
        rows = self._rows(owner_created_at)
        for sequence, _kind, state, _observed_at, before_raw, after_raw in rows:
            if state != "pending":
                continue
            before, after = self._decode(before_raw), self._decode(after_raw)
            if current == after:
                self._sync_visible_registry()
                self._set_state(sequence, "committed")
            elif current == before:
                self._set_state(sequence, "aborted")
            else:
                # An older writer may have advanced the registry without this
                # journal. The intent cannot be called committed or aborted.
                self._set_state(sequence, "uncertain")
        committed = [row for row in self._rows(owner_created_at) if row[2] == "committed"]
        if not committed:
            if current is not None:
                self._insert(owner_created_at, "baseline", "committed", None, current)
            return
        last = self._decode(committed[-1][5])
        if last != current:
            # Known endpoints, unknown intervening revisions. Never claim the
            # old writer's intermediate edits or timing as recorded history.
            self._insert(owner_created_at, "observed_gap", "committed", last, current)

    def begin(self, owner_created_at: float, before: Goal | None, after: Goal | None) -> int:
        self.observe(owner_created_at, before)
        return self._insert(owner_created_at, "transition", "pending", before, after)

    def commit(self, sequence: int) -> None:
        self._set_state(sequence, "committed")

    def history(
        self, owner_created_at: float, current: Goal | None, goal_id: str | None = None
    ) -> tuple[GoalHistoryEntry, ...]:
        self.observe(owner_created_at, current)
        entries: list[GoalHistoryEntry] = []
        for sequence, kind, state, observed_at, before_raw, after_raw in self._rows(
            owner_created_at
        ):
            if state != "committed":
                continue
            before, after = self._decode(before_raw), self._decode(after_raw)
            if goal_id is not None and goal_id not in {
                before.id if before is not None else None,
                after.id if after is not None else None,
            }:
                continue
            entries.append(GoalHistoryEntry(sequence, kind, observed_at, before, after))
        return tuple(entries)
