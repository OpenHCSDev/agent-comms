"""The registry as rows: one thread, status, alias or generation per row, so a change writes the
rows it changes and nothing else (plans/REGISTRY-ROWS-PLAN.md).

The whole-document registry.json rewrote every row on every change: 5.6 MB and 2,933 threads on
the live bus to change one. These tables hold the same facts the document's fields hold, keyed by
the name the document keys them by; the document's scalar fields are the one header row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.coordination.errors import SchemaVersionError
from agent_comms.registry.registry_document import RootPurpose, TeamProject
from agent_comms.registry.thread_status import ThreadStatus
from agent_comms.registry.threads import Thread
from agent_comms.storage.typed_table import (
    Column,
    DurableTables,
    FrozenColumn,
    Index,
    SharedFileTables,
    TypedTable,
)


class RegistryTable(SharedFileTables, DurableTables):
    """The registry's tables; each row family is one field of the old document."""

    schema_error = SchemaVersionError

    @classmethod
    def declarations(cls) -> tuple[type[TypedTable], ...]:
        return TypedTable.members_with(cls)

    @classmethod
    def schema_rows(cls) -> tuple[type[TypedTable], ...]:
        return cls.declarations()


class RegistryRow(RegistryTable, TypedTable, declared_name=None):
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
