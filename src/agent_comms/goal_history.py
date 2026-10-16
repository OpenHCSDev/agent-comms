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
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import ClassVar, Literal

from .coordination_database import CoordinationStore
from .field_codec import FieldCodec
from .goals import Goal
from .typed_table import Column, ExpressionIndex, Index, SQLiteSchemaObject, TypedTable, _identifier


class GoalHistoryError(RuntimeError):
    """Goal history could not be durably written or reconciled."""


@dataclass(frozen=True)
class GoalHistoryEntry(TypedTable):
    # The durable kind field is a journal fact, not the TypedTable family tag.
    family_discriminator = "row"

    sequence: int | None = field(metadata={"sql": Column(primary_key=True)})
    kind: Literal["transition", "baseline", "observed_gap"]
    observed_at: float
    before: Goal | None
    after: Goal | None
    owner_created_at: float = field(kw_only=True, metadata={"history_exclude": True})
    state: Literal["pending", "committed", "aborted", "uncertain"] = field(
        kw_only=True, metadata={"history_exclude": True}
    )
    BEFORE_GOAL_ID: ClassVar[str] = 'json_extract("before", \'$.id\')'
    AFTER_GOAL_ID: ClassVar[str] = 'json_extract("after", \'$.id\')'
    REPORTED_TURN: ClassVar[str] = 'json_extract("after", \'$.reported_turn\')'
    indexes = (
        Index(("owner_created_at", "sequence")),
        Index(("owner_created_at", "state", "sequence")),
        *(ExpressionIndex(("owner_created_at", "state", expression, "sequence"))
          for expression in (BEFORE_GOAL_ID, AFTER_GOAL_ID, REPORTED_TURN)),
        Index(("owner_created_at",), unique=True, where="state='pending'"),
    )

    @classmethod
    def missing_indexes(cls, actual: list[SQLiteSchemaObject]) -> dict[str, str]:
        """One row format; physical indexes may be absent in an original capture."""
        present = {item.name for item in actual}
        missing = {name: sql for name, sql in cls.index_schema().items() if name not in present}
        expected = {name: sql for name, sql in cls.schema_objects().items() if name not in missing}
        if SQLiteSchemaObject.differences(actual, expected):
            raise GoalHistoryError("Goal history differs beyond missing declared indexes.")
        return missing

    def reports_turn(self, turn_id: str) -> bool:
        """A committed goal revision records its original model reporting turn."""
        return self.after is not None and self.after.reported_turn == turn_id

    def to_wire(self) -> dict[str, object]:
        # Preserve nested Goal family tags while projecting only public entry fields.
        return {
            item.name: FieldCodec.encode(getattr(self, item.name))
            for item in fields(self)
            if not item.metadata.get("history_exclude")
        }


@dataclass(frozen=True, kw_only=True)
class GoalHistoryQuery:
    """One bounded newest-first page, shared by tool and runtime requests."""

    MAX_ROWS: ClassVar[int] = 100
    goal_id: str | None = field(default=None, metadata={"description": "Optional goal ID"})
    before_sequence: int | None = field(
        default=None, metadata={"description": "Read older entries before this sequence"},
    )
    limit: int = field(default=MAX_ROWS, metadata={"description": "Maximum entries in this page"})

    def __post_init__(self) -> None:
        if not 0 < self.limit <= self.MAX_ROWS:
            raise ValueError(f"Goal history limit must be between 1 and {self.MAX_ROWS}.")
        if self.before_sequence is not None and self.before_sequence <= 0:
            raise ValueError("Goal history before_sequence must be positive.")
        if self.goal_id is not None:
            object.__setattr__(self, "goal_id", self.goal_id.strip() or None)

    def read(self, connection: sqlite3.Connection, owner_created_at: float,
             through_sequence: int) -> tuple[GoalHistoryEntry, ...]:
        upper = through_sequence
        if self.before_sequence is not None:
            upper = min(upper, self.before_sequence - 1)
        table = _identifier(GoalHistoryEntry.declared_name)
        columns = GoalHistoryEntry._column_list(GoalHistoryEntry.columns())
        basis = "owner_created_at=? AND state='committed' AND sequence<=?"
        parameters = (owner_created_at, upper)
        if self.goal_id is None:
            statement = f"SELECT {columns} FROM {table} WHERE {basis} ORDER BY sequence DESC LIMIT ?"
            parameters = (*parameters, self.limit)
        else:
            # Limit each indexed endpoint before merging. A goal replacement belongs
            # to both goals; UNION removes that duplicate without scanning either past.
            matches = " UNION ".join(
                f"SELECT sequence FROM (SELECT sequence FROM {table} WHERE {basis} "
                f"AND {endpoint}=? ORDER BY sequence DESC LIMIT ?)"
                for endpoint in (GoalHistoryEntry.BEFORE_GOAL_ID, GoalHistoryEntry.AFTER_GOAL_ID)
            )
            statement = (
                f"SELECT {columns} FROM {table} WHERE sequence IN ({matches}) "
                "ORDER BY sequence DESC LIMIT ?"
            )
            parameters = (*(parameters + (self.goal_id, self.limit)) * 2, self.limit)
        return tuple(GoalHistoryEntry.read(connection.execute(statement, parameters)))


@dataclass(frozen=True)
class GoalHistoryRead:
    """The captured incarnation and committed cursor, not another current goal."""

    path: Path
    owner_created_at: float
    through_sequence: int | None

    def entries(self, query: GoalHistoryQuery = GoalHistoryQuery()) -> tuple[GoalHistoryEntry, ...]:
        if self.through_sequence is None:
            return ()
        try:
            with closing(CoordinationStore.read_snapshot(self.path)) as connection:
                return query.read(connection, self.owner_created_at, self.through_sequence)
        except (OSError, sqlite3.Error, ValueError, TypeError) as error:
            raise GoalHistoryError("Cannot read goal history.") from error

    def reports_turn(self, turn_id: str) -> bool:
        """Semantic report deduplication is exact, never a bounded display scan."""
        if self.through_sequence is None:
            return False
        try:
            with closing(CoordinationStore.read_snapshot(self.path)) as connection:
                return connection.execute(
                    f"SELECT 1 FROM {_identifier(GoalHistoryEntry.declared_name)} "
                    "WHERE owner_created_at=? AND state='committed' "
                    f"AND {GoalHistoryEntry.REPORTED_TURN}=? AND sequence<=? LIMIT 1",
                    (self.owner_created_at, turn_id, self.through_sequence),
                ).fetchone() is not None
        except (OSError, sqlite3.Error) as error:
            raise GoalHistoryError("Cannot read goal report history.") from error


class GoalHistoryStore:
    """One journal per registry root; callers hold the registry file lock."""

    def __init__(self, registry_path: Path):
        self.registry_path = registry_path
        self.path = registry_path.parent / "goal_history.sqlite3"
        self._initialize()

    @classmethod
    def acquire_read_only(cls, registry_path: Path) -> tuple[GoalHistoryEntry, ...]:
        """Acquire full original rows; caller holds original registry custody.

        This boundary never constructs a store, initializes a database, observes
        current goals or reconciles pending writes. Missing/foreign storage is a
        refusal, not permission to manufacture a history baseline.
        """
        path = registry_path.parent / "goal_history.sqlite3"
        try:
            before = path.lstat()
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) != 0o600):
                raise GoalHistoryError("Original goal history must be a private regular file.")
            with closing(CoordinationStore.read_snapshot(path)) as connection:
                actual = SQLiteSchemaObject.read(connection.execute(
                    "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                    "AND name NOT LIKE 'sqlite_%'"
                ))
                GoalHistoryEntry.missing_indexes(actual)
                rows = tuple(GoalHistoryEntry.select(connection, order_by=("sequence",)))
            after = path.lstat()
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise GoalHistoryError("Original goal history changed during acquisition.")
            return rows
        except (OSError, sqlite3.Error, ValueError, TypeError) as error:
            raise GoalHistoryError("Cannot acquire original goal history read-only.") from error

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = CoordinationStore.writer(self.path, isolation_level=None)
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
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
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
                elif GoalHistoryEntry.missing_indexes(actual):
                    raise GoalHistoryError("Goal history requires the one-shot index installation.")
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

    def _latest(self, owner_created_at: float, *, state: str | None = None) -> GoalHistoryEntry | None:
        try:
            with closing(self._connect()) as connection:
                where, parameters = "owner_created_at=?", (owner_created_at,)
                if state is not None:
                    where += " AND state=?"
                    parameters += (state,)
                rows = GoalHistoryEntry.read(connection.execute(
                    f"SELECT {GoalHistoryEntry._column_list(GoalHistoryEntry.columns())} "
                    f"FROM {_identifier(GoalHistoryEntry.declared_name)} "
                    f"WHERE {where} ORDER BY sequence DESC LIMIT 1", parameters,
                ))
                return next(iter(rows), None)
        except (sqlite3.Error, ValueError, TypeError) as error:
            raise GoalHistoryError("Cannot read goal history tail.") from error

    def observe(self, owner_created_at: float, current: Goal | None) -> GoalHistoryRead:
        """Reconcile the sole unfinished tail and capture a committed read cursor.

        begin() settles the previous tail before appending another intent while
        its caller holds the registry lock. No earlier entry can remain pending.
        """
        last = self._latest(owner_created_at)
        if last is not None and last.state == "pending":
            if current == last.after:
                self._sync_visible_registry()
                state = "committed"
            elif current == last.before:
                state = "aborted"
            else:
                state = "uncertain"
            self._set_state(last.sequence, state)
            last = replace(last, state=state)
        if last is not None and last.state != "committed":
            last = self._latest(owner_created_at, state="committed")
        sequence = last.sequence if last is not None else None
        if last is None:
            if current is not None:
                sequence = self._insert(owner_created_at, "baseline", "committed", None, current)
        elif last.after != current:
            # Known endpoints do not manufacture unobserved revisions or timing.
            sequence = self._insert(owner_created_at, "observed_gap", "committed", last.after, current)
        return GoalHistoryRead(self.path, owner_created_at, sequence)

    def begin(self, owner_created_at: float, before: Goal | None, after: Goal | None) -> int:
        self.observe(owner_created_at, before)
        return self._insert(owner_created_at, "transition", "pending", before, after)

    def commit(self, sequence: int) -> None:
        self._set_state(sequence, "committed")
