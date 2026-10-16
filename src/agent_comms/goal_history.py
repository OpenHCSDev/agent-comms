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
from abc import abstractmethod
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import ClassVar, Literal

from .coordination_database import CoordinationStore
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .goal_history_state import Committed, GoalHistoryState, Pending
from .goals import Goal
from .typed_table import Column, ExpressionIndex, Index, SQLiteSchemaObject, TypedTable


class GoalHistoryError(RuntimeError):
    """Goal history could not be durably written or reconciled."""


@dataclass(frozen=True)
class GoalHistoryRevision:
    """The public revision shared by storage and decoded history consumers."""

    sequence: int | None = field(metadata={"sql": Column(primary_key=True)})
    kind: Literal["transition", "baseline", "observed_gap"]
    observed_at: float
    before: Goal | None
    after: Goal | None


@dataclass(frozen=True)
class GoalHistoryEntry(GoalHistoryRevision, TypedTable):
    # The durable kind field is a journal fact, not the TypedTable family tag.
    family_discriminator = "row"

    owner_created_at: float = field(kw_only=True)
    state: type[GoalHistoryState] = field(kw_only=True)
    BEFORE_GOAL_ID: ClassVar[str] = 'json_extract("before", \'$.id\')'
    AFTER_GOAL_ID: ClassVar[str] = 'json_extract("after", \'$.id\')'
    REPORTED_TURN: ClassVar[str] = 'json_extract("after", \'$.reported_turn\')'
    indexes = (
        Index(("owner_created_at", "sequence")),
        Index(("owner_created_at", "state", "sequence")),
        *(ExpressionIndex(("owner_created_at", "state", expression, "sequence"))
          for expression in (BEFORE_GOAL_ID, AFTER_GOAL_ID, REPORTED_TURN)),
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
        public = GoalHistoryRevision(**{
            item.name: getattr(self, item.name) for item in fields(GoalHistoryRevision)
        })
        return FieldCodec.encode(public)


class GoalHistoryScope(DeclaredFamily):
    @abstractmethod
    def read(self, connection: sqlite3.Connection, owner_created_at: float,
             through_sequence: int, limit: int) -> tuple[GoalHistoryEntry, ...]: ...


@dataclass(frozen=True)
class AllGoals(GoalHistoryScope):
    def read(self, connection: sqlite3.Connection, owner_created_at: float,
             through_sequence: int, limit: int) -> tuple[GoalHistoryEntry, ...]:
        return tuple(GoalHistoryEntry.select(
            connection, where="sequence<=?", parameters=(through_sequence,),
            owner_created_at=owner_created_at, state=Committed,
            order_by=("sequence",), descending=True, limit=limit,
        ))


@dataclass(frozen=True)
class OneGoal(GoalHistoryScope):
    goal_id: str

    def __post_init__(self) -> None:
        selected = self.goal_id.strip()
        if not selected:
            raise ValueError("One goal history scope requires a goal ID.")
        object.__setattr__(self, "goal_id", selected)

    def read(self, connection: sqlite3.Connection, owner_created_at: float,
             through_sequence: int, limit: int) -> tuple[GoalHistoryEntry, ...]:
        # Both endpoints seek and limit before UNION; replacements belong to both
        # goals, and the table owns projection, union and outer selection SQL.
        queries = tuple(GoalHistoryEntry.select_statement(
            ("sequence",), where=f"sequence<=? AND {endpoint}=?",
            parameters=(through_sequence, self.goal_id),
            owner_created_at=owner_created_at, state=Committed,
            order_by=("sequence",), descending=True, limit=limit,
        ) for endpoint in (GoalHistoryEntry.BEFORE_GOAL_ID, GoalHistoryEntry.AFTER_GOAL_ID))
        return tuple(GoalHistoryEntry.select_union(
            connection, column="sequence", queries=queries,
            order_by=("sequence",), descending=True, limit=limit,
        ))


class GoalHistoryCursor(DeclaredFamily):
    @abstractmethod
    def upper_bound(self, through_sequence: int) -> int: ...


@dataclass(frozen=True)
class Newest(GoalHistoryCursor):
    def upper_bound(self, through_sequence: int) -> int:
        return through_sequence


@dataclass(frozen=True)
class Before(GoalHistoryCursor):
    sequence: int

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValueError("Goal history before sequence must be positive.")

    def upper_bound(self, through_sequence: int) -> int:
        return min(through_sequence, self.sequence - 1)


@dataclass(frozen=True, kw_only=True)
class GoalHistoryQuery:
    """One bounded newest-first page, shared by tool and runtime requests."""

    MAX_ROWS: ClassVar[int] = 100
    scope: GoalHistoryScope = field(default=AllGoals(), metadata={"description": "Goal selection"})
    cursor: GoalHistoryCursor = field(default=Newest(), metadata={"description": "Page position"})
    limit: int = field(default=MAX_ROWS, metadata={"description": "Maximum entries in this page"})

    def __post_init__(self) -> None:
        if not 0 < self.limit <= self.MAX_ROWS:
            raise ValueError(f"Goal history limit must be between 1 and {self.MAX_ROWS}.")

    def read(self, connection: sqlite3.Connection, owner_created_at: float,
             through_sequence: int) -> tuple[GoalHistoryEntry, ...]:
        return self.scope.read(
            connection, owner_created_at, self.cursor.upper_bound(through_sequence), self.limit,
        )


class GoalHistoryRead(DeclaredFamily):
    """The captured history cut owns whether any database may be consulted."""

    @abstractmethod
    def entries(self, query: GoalHistoryQuery = GoalHistoryQuery()) -> tuple[GoalHistoryEntry, ...]: ...

    @abstractmethod
    def reports_turn(self, turn_id: str) -> bool: ...


@dataclass(frozen=True)
class NoHistory(GoalHistoryRead):
    def entries(self, query: GoalHistoryQuery = GoalHistoryQuery()) -> tuple[GoalHistoryEntry, ...]:
        return ()

    def reports_turn(self, turn_id: str) -> bool:
        return False


@dataclass(frozen=True)
class ThroughSequence(GoalHistoryRead):
    path: Path
    owner_created_at: float
    through_sequence: int

    def __post_init__(self) -> None:
        if self.through_sequence <= 0:
            raise ValueError("A retained goal history cut requires a positive sequence.")

    @contextmanager
    def _opened(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(CoordinationStore.read_snapshot(self.path)) as connection:
                yield connection
        except (OSError, sqlite3.Error) as error:
            raise GoalHistoryError("Cannot read goal history.") from error

    def entries(self, query: GoalHistoryQuery = GoalHistoryQuery()) -> tuple[GoalHistoryEntry, ...]:
        with self._opened() as connection:
            return query.read(connection, self.owner_created_at, self.through_sequence)

    def reports_turn(self, turn_id: str) -> bool:
        """Semantic report lookup is exact, independent of the display page."""
        with self._opened() as connection:
            return bool(GoalHistoryEntry.select(
                connection, where=f"{GoalHistoryEntry.REPORTED_TURN}=? AND sequence<=?",
                parameters=(turn_id, self.through_sequence),
                owner_created_at=self.owner_created_at, state=Committed,
                order_by=("sequence",), descending=True, limit=1,
            ))


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
        except (OSError, sqlite3.Error) as error:
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
        state: type[GoalHistoryState],
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

    def set_state(self, sequence: int, state: type[GoalHistoryState]) -> None:
        with self._transaction() as connection:
            GoalHistoryEntry.update(
                connection, where="sequence=? AND state=?",
                parameters=(sequence, FieldCodec.encode(Pending)), state=state,
            )

    def sync_visible_registry(self) -> None:
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

    def _rows(
        self, owner_created_at: float, *, state: type[GoalHistoryState],
        limit: int | None = None, descending: bool = False,
    ) -> list[GoalHistoryEntry]:
        """Read the requested rows; no implicit cap on original or pending history."""
        try:
            with closing(self._connect()) as connection:
                return GoalHistoryEntry.select(
                    connection, owner_created_at=owner_created_at, state=state,
                    order_by=("sequence",), descending=descending, limit=limit,
                )
        except sqlite3.Error as error:
            raise GoalHistoryError("Cannot read goal history.") from error

    def observe(self, owner_created_at: float, current: Goal | None) -> GoalHistoryRead:
        """Reconcile every pending intent, then read only the latest committed row.

        The state index makes retained settled history irrelevant to transition
        work. Pending evidence is never clipped or rejected by a new cardinality
        constraint, including when an original journal has several pending rows.
        """
        for row in self._rows(owner_created_at, state=Pending):
            row.state.reconcile(row, self, current)
        last = next(iter(self._rows(
            owner_created_at, state=Committed, limit=1, descending=True,
        )), None)
        if last is None:
            if current is None:
                return NoHistory()
            sequence = self._insert(owner_created_at, "baseline", Committed, None, current)
        elif last.after != current:
            # Known endpoints do not manufacture unobserved revisions or timing.
            sequence = self._insert(owner_created_at, "observed_gap", Committed, last.after, current)
        else:
            sequence = last.sequence
        return ThroughSequence(self.path, owner_created_at, sequence)

    def begin(self, owner_created_at: float, before: Goal | None, after: Goal | None) -> int:
        self.observe(owner_created_at, before)
        return self._insert(owner_created_at, "transition", Pending, before, after)

    def commit(self, sequence: int) -> None:
        self.set_state(sequence, Committed)
