"""Durable goal revision history alongside the registry.

The registry Goal remains the current-state authority. A history intent is
synced before that registry changes; only a synced commit, or a later guarded
reconciliation against the registry, makes the transition readable. Registry
snapshots can change without erasing this journal.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Annotated

from agent_comms.field_codec import WireOmitDefault, GoalId
from agent_comms.owner_requests import NoRequestRevision, RequestAtRevision, RequestRevision

from .child_process import Platform
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, HistoryExclude, MalformedRecord
from .goals import GoalDeclaration, NoGoal
from .locked_store import DurableFile
from .private_registry_guard import PrivateRegistryGuard
from .store_files import AtomicFile
from .reported_turn import ReportedTurnState
from .typed_table import (
    Column,
    DurableTables,
    Index,
    JsonStorage,
    ReconciledFile,
    TypedTable,
)


class GoalHistoryError(RuntimeError):
    """Goal history could not be durably written or reconciled."""


class JournalState(DeclaredFamily, affix="Entry"):
    """A journal state's reconciliation and visibility belong to its declaration."""

    @classmethod
    def reconcile(
        cls, history: GoalHistoryStore, row: GoalHistoryEntry, current: GoalDeclaration
    ) -> None:
        """Resolved journal entries need no reconciliation."""

    @classmethod
    def recorded(cls, row: GoalHistoryEntry) -> tuple[GoalHistoryEntry, ...]:
        """Only committed entries become recorded history."""
        return ()


class PendingEntry(JournalState):
    @classmethod
    def reconcile(
        cls, history: GoalHistoryStore, row: GoalHistoryEntry, current: GoalDeclaration
    ) -> None:
        if current == row.after:
            history._sync_visible_registry()
            history._set_state(row.sequence, CommittedEntry)
        elif current == row.before:
            history._set_state(row.sequence, AbortedEntry)
        else:
            # A changed registry is not evidence that this intent committed.
            history._set_state(row.sequence, UncertainEntry)


class CommittedEntry(JournalState):
    @classmethod
    def recorded(cls, row: GoalHistoryEntry) -> tuple[GoalHistoryEntry, ...]:
        return (row,)


class AbortedEntry(JournalState):
    """The registry retained the intent's previous value."""


class UncertainEntry(JournalState):
    """The registry proves neither endpoint of the intent."""


class RevisionKind(DeclaredFamily, affix="Revision"):
    """The declared reason a goal revision entered the journal."""


class TransitionRevision(RevisionKind):
    """A journaled change from one goal declaration to another."""


class BaselineRevision(RevisionKind):
    """The current goal first observed beside an existing registry."""


class ObservedGapRevision(RevisionKind):
    """Observed endpoints without an attested intervening transition."""


@dataclass(frozen=True, kw_only=True)
class RequestHistory:
    """Moved request data stays linked to its original history observation."""

    request_before: Annotated[
        RequestRevision, Column(storage=JsonStorage, nullable=True), WireOmitDefault(),
    ] = NoRequestRevision()
    request_after: Annotated[
        RequestRevision, Column(storage=JsonStorage, nullable=True), WireOmitDefault(),
    ] = NoRequestRevision()

    @classmethod
    def endpoint_columns(cls) -> dict[str, str]:
        """Request-prefix fields declare the goal endpoint each preserves."""
        return {entry.name.removeprefix("request_"): entry.name for entry in fields(RequestHistory)}

    @property
    def request_revisions(self) -> tuple[RequestRevision, ...]:
        return tuple(getattr(self, entry.name) for entry in fields(RequestHistory))


@dataclass(frozen=True)
class RecordedGoalRevision(RequestHistory):
    """A history entry's public fields; its owner key and journal state stay in the store."""
    sequence: Annotated[int, Column(primary_key=True)]
    kind: type[RevisionKind]
    observed_at: float
    before: Annotated[GoalDeclaration, Column(storage=JsonStorage, nullable=True)]
    after: Annotated[GoalDeclaration, Column(storage=JsonStorage, nullable=True)]


@dataclass(frozen=True)
class GoalHistoryEntry(RecordedGoalRevision, TypedTable, DurableTables):
    # The durable kind field is a journal fact, not the TypedTable family tag.
    schema_error = GoalHistoryError

    owner_created_at: Annotated[float, HistoryExclude()] = field(
        kw_only=True
    )
    state: Annotated[type[JournalState], HistoryExclude()] = field(kw_only=True)
    indexes = (Index(("owner_created_at", "sequence")),)

    @classmethod
    def orders_with_replacement(
        cls, db: sqlite3.Connection, column: str, replacement: str, *,
        order_kind: str, where: str = "1",
    ) -> sqlite3.Cursor:
        """Read matching endpoint orders through this table's column declaration."""
        selected = cls._column_list((column,))
        return db.execute(
            f'SELECT {cls._column_list(("sequence", "observed_at", "owner_created_at"))}, '
            f"json_set({selected}, '$.order', json(?)) AS goal, "
            f"coalesce(json_extract({selected}, '$.order.directives'), '[]') AS directives "
            f'FROM "{cls.declared_name}" '
            f"WHERE json_extract({selected}, '$.order.kind')=? AND ({where}) "
            f'ORDER BY {cls._column_list(("sequence",))}', (replacement, order_kind))

    @classmethod
    def move_request_order(
        cls, db: sqlite3.Connection, column: str, sequence: int,
        request: RequestAtRevision, replacement: str,
    ) -> None:
        """Move an endpoint's request to its prefix without dropping the goal observation."""
        goal_column = cls._column_list((column,))
        prefix_column = cls._column_list((cls.endpoint_columns()[column],))
        db.execute(
            f'UPDATE "{cls.declared_name}" '
            f"SET {goal_column}=json_set({goal_column}, '$.order', json(?)), {prefix_column}=? "
            f'WHERE {cls._column_list(("sequence",))}=?',
            (replacement, json.dumps(FieldCodec.encode(request)), sequence))

    def reports_turn(self, turn: ReportedTurnState) -> bool:
        """A committed goal revision records its original model reporting turn."""
        return self.after.reports_turn(turn)

    def recorded(self) -> RecordedGoalRevision:
        return FieldCodec.construct(RecordedGoalRevision, {
            entry.name: getattr(self, entry.name) for entry in fields(RecordedGoalRevision)
        })

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode_record(self.recorded())


class GoalHistoryStore(DurableFile, ReconciledFile):
    """One journal per registry root; callers hold the registry file lock."""

    filename = "goal_history.sqlite3"

    @classmethod
    def table_schema(cls) -> type[GoalHistoryEntry]:
        return GoalHistoryEntry

    def __init__(self, registry_path: Path):
        self.registry_path = registry_path
        self.path = self.path_in(registry_path.parent)
        self._initialize()

    @classmethod
    def acquire_read_only(cls, registry_path: Path) -> tuple[GoalHistoryEntry, ...]:
        """Read the full stored rows while the caller holds the registry lock.

        This boundary never constructs a store, initializes a database, observes
        current goals or reconciles pending writes. Missing/foreign storage is a
        refusal, not permission to manufacture a history baseline.
        """
        path = cls.path_in(registry_path.parent)
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or not Platform.current().is_private(before, 0o600):
                raise GoalHistoryError("Original goal history must be a private regular file.")
            with closing(cls.read_snapshot(path)) as connection:
                GoalHistoryEntry.require_schema(connection)
                rows = tuple(GoalHistoryEntry.select(connection, order_by=("sequence",)))
            after = path.lstat()
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise GoalHistoryError("Original goal history changed during acquisition.")
            return rows
        except (OSError, sqlite3.Error, MalformedRecord) as error:
            raise GoalHistoryError("Cannot acquire original goal history read-only.") from error

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = self.writer(self.path, isolation_level=None)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=EXTRA")
            return connection
        except sqlite3.Error as error:
            raise GoalHistoryError("Goal history database is unavailable.") from error

    def _sync(self) -> None:
        AtomicFile(self.path).sync()

    def _initialize(self) -> None:
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        except OSError as error:
            raise GoalHistoryError("Cannot create goal history database.") from error
        else:
            os.close(descriptor)
        if not Platform.current().is_private(self.path.stat(), 0o600):
            raise GoalHistoryError("Goal history database must be owner-only.")
        try:
            with closing(self._connect()) as connection:
                self.table_schema().install_schema(connection, self.schema_change(self.path))
            self._sync()
        except (OSError, sqlite3.Error) as error:
            raise GoalHistoryError("Goal history initialization is uncertain.") from error

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Share durable commit mechanics without inventing an operation result."""
        try:
            with closing(self._connect()) as connection, self.write_transaction(connection):
                yield connection
            self._sync()
        except (OSError, sqlite3.Error) as error:
            raise GoalHistoryError("Goal history write durability is uncertain.") from error

    def _insert(
        self,
        owner_created_at: float,
        kind: type[RevisionKind],
        state: type[JournalState],
        before: GoalDeclaration,
        after: GoalDeclaration,
    ) -> int:
        with self._transaction() as connection:
            # SQLite assigns the sequence; a stored entry always has one.
            cursor = GoalHistoryEntry.insert_values(
                connection,
                kind=kind,
                observed_at=time.time(),
                before=before,
                after=after,
                owner_created_at=owner_created_at,
                state=state,
            )
            sequence = cursor.lastrowid
            if sequence is None:
                raise GoalHistoryError("Goal history insert returned no sequence.")
        return sequence

    def _set_state(
        self, sequence: int, state: type[JournalState]
    ) -> None:
        with self._transaction() as connection:
            GoalHistoryEntry.update(
                connection,
                where="sequence=? AND state=?",
                parameters=(
                    sequence, GoalHistoryEntry._fields_named(("state",))[0].encode(PendingEntry)
                ),
                state=state,
            )

    def _sync_visible_registry(self) -> None:
        """Adopt a visible post-crash registry value before attesting an intent."""
        targets = [self.registry_path]
        guard = PrivateRegistryGuard.path_in(self.registry_path.parent)
        if guard.exists():
            targets.append(guard)
        try:
            for target in targets:
                AtomicFile(target).sync()
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
        except (sqlite3.Error, MalformedRecord) as error:
            raise GoalHistoryError("Cannot read goal history.") from error

    def observe(self, owner_created_at: float, current: GoalDeclaration) -> None:
        """Reconcile uncertain writes and label unjournaled changes truthfully."""
        for row in self._rows(owner_created_at):
            row.state.reconcile(self, row, current)
        committed = [
            entry for row in self._rows(owner_created_at) for entry in row.state.recorded(row)
        ]
        if not committed:
            for goal in current:
                self._insert(owner_created_at, BaselineRevision, CommittedEntry, NoGoal(), goal)
            return
        last = committed[-1].after
        if last != current:
            # Known endpoints do not manufacture unobserved revisions or timing.
            self._insert(owner_created_at, ObservedGapRevision, CommittedEntry, last, current)

    def begin(self, owner_created_at: float, before: GoalDeclaration, after: GoalDeclaration) -> int:
        self.observe(owner_created_at, before)
        return self._insert(owner_created_at, TransitionRevision, PendingEntry, before, after)

    def commit(self, sequence: int) -> None:
        self._set_state(sequence, CommittedEntry)

    def history(
        self, owner_created_at: float, current: GoalDeclaration, goal_id: GoalId | None = None
    ) -> tuple[GoalHistoryEntry, ...]:
        self.observe(owner_created_at, current)
        return tuple(
            row
            for entry in self._rows(owner_created_at)
            for row in entry.state.recorded(entry)
            if (
                goal_id is None
                or goal_id
                in {
                    row.before.reported_id(),
                    row.after.reported_id(),
                }
            )
        )
