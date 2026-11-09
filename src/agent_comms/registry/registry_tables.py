"""The registry as rows: one thread, status, alias or generation per row, so a change writes the
rows it changes and nothing else (plans/REGISTRY-ROWS-PLAN.md).

The whole-document registry.json rewrote every row on every change: 5.6 MB and 2,933 threads on
the live bus to change one. These tables hold the same facts the document's fields hold, keyed by
the name the document keys them by; the document's scalar fields are the one header row.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, ClassVar

from agent_comms.registry.registry_document import RootPurpose, TeamProject
from agent_comms.registry.thread_status import ThreadStatus
from agent_comms.registry.threads import Thread
from agent_comms.storage.typed_table import (
    Column,
    DurableStore,
    FrozenColumn,
    Index,
    StoreRows,
    TypedTable,
)


class RegistryRows(StoreRows):
    """The registry store discovers its rows through this membership declaration."""


@dataclass(frozen=True, slots=True)
class RegistryRowStore(DurableStore):
    """The registry's rows, one owner-only SQLite file beside the bus: every installation
    carries it, and nothing else can rebuild it."""

    root: Path
    filename: ClassVar[str] = "registry.sqlite3"
    tables = RegistryRows

    def revision(self) -> int:
        with self.snapshot() as db:
            return self.revision_in(db)

    @staticmethod
    def revision_in(db: sqlite3.Connection) -> int:
        """The last committed revision; 0 before the first commit."""
        return max((header.revision for header in RegistryHeader.select(db)), default=0)

    def commit(
        self, header: RegistryHeader, rows: Iterable[RegistryNamedRow],
        removals: Iterable[tuple[type[RegistryNamedRow], str]],
    ) -> int:
        """Write only these rows and removals, stamped with the next revision, in one
        transaction: the cost is the change, never the registry's size."""
        with self.writing() as db:
            revision = self.revision_in(db) + 1
            replace(header, revision=revision).upsert(db)
            for row in rows:
                replace(row, changed_at=revision).upsert(db)
            for family, name in removals:
                family.delete(db, **{family.key_field(): name})
                RegistryRemovals(changed_at=revision, family=family, name=name).upsert(db)
            return revision

    def changes_since(self, revision: int) -> RegistryChanges:
        """Every row written and every row removed after ``revision``, read in one snapshot."""
        with self.snapshot() as db:
            return RegistryChanges(
                revision=self.revision_in(db),
                rows=tuple(
                    row for family in RegistryNamedRow.families()
                    for row in family.select(db, where="changed_at > ?", parameters=(revision,))
                ),
                removals=tuple(RegistryRemovals.select(
                    db, where="changed_at > ?", parameters=(revision,))),
            )


class RegistryRow(RegistryRows, TypedTable, declared_name=None):
    """A row of the registry."""


@dataclass(frozen=True)
class RevisionStamped(RegistryRow, declared_name=None):
    """A row stamped with the revision that last wrote it, so a reader holding revision r reads
    only the rows with changed_at > r."""

    changed_at: Annotated[int, Column(check="changed_at > 0")]
    indexes = (Index(("changed_at",), unique=False, where=None),)


@dataclass(frozen=True)
class RegistryNamedRow(RevisionStamped, declared_name=None):
    """A row keyed by a thread name or alias."""

    @classmethod
    def key_field(cls) -> str:
        """The one field the row declares as its primary key."""
        (key,) = (item.name for item in cls._fields if item.column.primary_key)
        return key

    @classmethod
    def families(cls) -> tuple[type[RegistryNamedRow], ...]:
        return tuple(member for member in TypedTable.members_with(RegistryRows)
                     if issubclass(member, RegistryNamedRow))


@dataclass(frozen=True)
class RegistryHeader(RegistryRow):
    """The document's scalar fields: one row, keyed by a constant."""

    header: Annotated[int, FrozenColumn(primary_key=True, check="header = 1")]
    revision: Annotated[int, Column(check="revision >= 0")]
    purpose: RootPurpose
    team_project: TeamProject
    owner_counter: Annotated[int, Column(check="owner_counter >= 0")]
    claim_counter: Annotated[int, Column(check="claim_counter >= 0")]


@dataclass(frozen=True)
class RegistryThreads(RegistryNamedRow):
    """A thread's declaration, under its name."""

    name: Annotated[str, FrozenColumn(primary_key=True)]
    thread: Thread


@dataclass(frozen=True)
class RegistryStatuses(RegistryNamedRow):
    """A thread's status, under its name."""

    name: Annotated[str, FrozenColumn(primary_key=True)]
    status: ThreadStatus


@dataclass(frozen=True)
class RegistryAliases(RegistryNamedRow):
    """An alias and the thread name it resolves to."""

    alias: Annotated[str, FrozenColumn(primary_key=True)]
    name: str


@dataclass(frozen=True)
class RegistryOwnerGenerations(RegistryNamedRow):
    """A name's owner generation; deleted names keep theirs as a tombstone."""

    name: Annotated[str, FrozenColumn(primary_key=True)]
    generation: Annotated[int, Column(check="generation > 0")]


@dataclass(frozen=True)
class RegistryClaimGenerations(RegistryNamedRow):
    """A name's claim generation; deleted names keep theirs as a tombstone."""

    name: Annotated[str, FrozenColumn(primary_key=True)]
    generation: Annotated[int, Column(check="generation > 0")]


@dataclass(frozen=True)
class RegistryRemovals(RevisionStamped):
    """A row removed at a revision: incremental readers learn deletions here, since a deleted
    row can't answer ``changed_at > r`` itself. The family is the row class, not a name for it."""

    family: Annotated[type[RegistryNamedRow], FrozenColumn(primary_key=True)]
    name: Annotated[str, FrozenColumn(primary_key=True)]


@dataclass(frozen=True)
class RegistryChanges:
    """What changed after a revision: the rows written and the rows removed."""

    revision: int
    rows: tuple[RegistryNamedRow, ...]
    removals: tuple[RegistryRemovals, ...]
