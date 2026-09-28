"""Durable goal revision history alongside the registry.

The registry Goal remains the current-state authority. A history intent is
synced before that registry changes; only a synced commit, or a later guarded
reconciliation against the registry, makes the transition readable. Registry
snapshots can change without erasing this journal.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Literal

from .field_codec import FieldCodec
from .goals import Goal
from .typed_table import Column, Index, SQLiteSchemaObject, TypedTable


class GoalHistoryError(RuntimeError):
    """Goal history could not be durably written or reconciled."""


@dataclass(frozen=True)
class GoalHistoryEntry(TypedTable):
    sequence: int | None = field(metadata={"sql": Column(primary_key=True)})
    kind: Literal["transition", "baseline", "observed_gap"]
    observed_at: float
    before: Goal | None
    after: Goal | None
    owner_created_at: float = field(kw_only=True, metadata={"history_exclude": True})
    state: Literal["pending", "committed", "aborted", "uncertain"] = field(
        kw_only=True, metadata={"history_exclude": True}
    )
    indexes = (Index(("owner_created_at", "sequence")),)

    def to_wire(self) -> dict[str, object]:
        # Preserve nested Goal family tags while projecting only public entry fields.
        return {
            item.name: FieldCodec.encode(getattr(self, item.name))
            for item in fields(self)
            if not item.metadata.get("history_exclude")
        }


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
        # Windows' CRT _commit (used by os.fsync) rejects a read-only fd.
        # Retain the existing read-only POSIX reopen semantics.
        descriptor = os.open(path, os.O_RDWR if os.name == "nt" else os.O_RDONLY)
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
                actual = SQLiteSchemaObject.read(
                    connection.execute(
                        "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                )
                if not actual:
                    GoalHistoryEntry.create(connection)
                elif {row.name: row.sql for row in actual} != GoalHistoryEntry.schema_objects():
                    raise GoalHistoryError("Goal history requires the one-shot durable cutover.")
                connection.commit()
            self._sync()
        except (OSError, sqlite3.Error) as error:
            raise GoalHistoryError("Goal history initialization is uncertain.") from error

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Share durable commit mechanics without inventing an operation result."""
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            self._sync()
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
        with self._transaction() as connection:
            cursor = GoalHistoryEntry(
                None,
                kind,
                time.time(),
                before,
                after,
                owner_created_at=owner_created_at,
                state=state,
            ).insert(connection)
            sequence = cursor.lastrowid
            if sequence is None:
                raise GoalHistoryError("Goal history insert returned no sequence.")
        return sequence

    def _set_state(
        self, sequence: int, state: Literal["committed", "aborted", "uncertain"]
    ) -> None:
        with self._transaction() as connection:
            GoalHistoryEntry.update(
                connection,
                where="sequence=? AND state='pending'",
                parameters=(sequence,),
                state=state,
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

    def _rows(self, owner_created_at: float) -> list[GoalHistoryEntry]:
        try:
            with closing(self._connect()) as connection:
                return GoalHistoryEntry.select(
                    connection,
                    where="owner_created_at=?",
                    parameters=(owner_created_at,),
                    order_by=("sequence",),
                )
        except (sqlite3.Error, ValueError, TypeError) as error:
            raise GoalHistoryError("Cannot read goal history.") from error

    def observe(self, owner_created_at: float, current: Goal | None) -> None:
        """Reconcile uncertain writes and label unjournaled changes truthfully."""
        for row in self._rows(owner_created_at):
            if row.state != "pending":
                continue
            if current == row.after:
                self._sync_visible_registry()
                self._set_state(row.sequence, "committed")
            elif current == row.before:
                self._set_state(row.sequence, "aborted")
            else:
                # A changed registry is not evidence that this intent committed.
                self._set_state(row.sequence, "uncertain")
        committed = [row for row in self._rows(owner_created_at) if row.state == "committed"]
        if not committed:
            if current is not None:
                self._insert(owner_created_at, "baseline", "committed", None, current)
            return
        last = committed[-1].after
        if last != current:
            # Known endpoints do not manufacture unobserved revisions or timing.
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
        return tuple(
            row
            for row in self._rows(owner_created_at)
            if row.state == "committed"
            and (
                goal_id is None
                or goal_id
                in {
                    row.before.id if row.before is not None else None,
                    row.after.id if row.after is not None else None,
                }
            )
        )
