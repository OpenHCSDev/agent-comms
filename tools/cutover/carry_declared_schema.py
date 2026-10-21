"""Carry durable SQLite files onto their declared schema once; keep every row.

Files written before index names were derived from what each index covers hold
positional names (delivery_0_idx), and files written before every file kind was
compared with its declaration hold a sealed copy of it: a meta table with a
digest or version and PRAGMA user_version. Each file is converted in place inside
one write transaction: indexes and views come from the declarations of the tables
it holds, the sealed copies go, and
every table keeps its rows.

The files are every durable SQLite file the bus declares and every saved session's
input proof under the bus root; a file whose tables all rebuild themselves is left to rebuild. Delete
once every bus has run it.
"""

from __future__ import annotations

import re
from contextlib import closing
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from pkgutil import walk_packages
from typing import ClassVar

import agent_comms
from agent_comms.coordination_database import CoordinationStore
from agent_comms.declared_family import DeclaredFamily, UnknownFamilyMember
from agent_comms.errors import RelationViolationError
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import DurableFile, SQLiteFile
from agent_comms.typed_table import (
    DerivedTables,
    SQLiteSchemaObject,
    SQLiteUserVersion,
    TypedRow,
    TypedTable,
)

# The sealed copies the declarations replaced; their classes are deleted, so only
# their stored names remain.
RETIRED_TABLES = frozenset({
    "schema_meta",
    "cohort_schema_meta",
    "awareness_schema_meta",
    "native_runtime_schema_meta",
    "response_schema_meta",
    "goal_attempt_schema",
})

# Every declaration, so a stored table is looked up among all of them and an object
# another file kind's row declares is never mistaken for a stray.
for _module in walk_packages(agent_comms.__path__, "agent_comms."):
    import_module(_module.name)


_INDEX_NAME = re.compile(r'^(CREATE (?:UNIQUE )?INDEX )(?:"(?:[^"]|"")+"|\S+)( ON )')


def unnamed(sql: str) -> str:
    """An index's SQL without its name: what it covers."""
    return _INDEX_NAME.sub(r"\1\2", sql, count=1)


class CatalogKind(DeclaredFamily, affix="Kind"):
    """What SQLite's catalog calls an entry; each kind owns how the carry treats it."""

    holds_rows: ClassVar[bool] = False
    named_by_coverage: ClassVar[bool] = False


class TableKind(CatalogKind):
    holds_rows = True


class IndexKind(CatalogKind):
    named_by_coverage = True


class ViewKind(CatalogKind):
    pass


@dataclass(frozen=True)
class StoredObject(TypedRow):
    """One catalog entry: SQLite's own type, name and table."""

    type: type[CatalogKind]
    name: str
    tbl_name: str
    sql: str

    @property
    def retired(self) -> bool:
        return self.type.holds_rows and self.name in RETIRED_TABLES

    def drop(self, db) -> None:
        db.execute(f'DROP {self.type.declared_name.upper()} IF EXISTS "{self.name}"')

    def declaration(self, path: Path) -> type[TypedTable]:
        try:
            return TypedTable.member_named(self.name)
        except UnknownFamilyMember:
            raise RelationViolationError(f"{path}: undeclared table {self.name}") from None

    def row(self, path: Path) -> type[TypedTable]:
        """The declaration of this stored table, refusing one the carry cannot keep."""
        row = self.declaration(path)
        if derived(row):
            raise RelationViolationError(f"{path}: {self.name} is derived; it rebuilds, never carried")
        if self.sql != row.structure_objects()[row.declared_name]:
            raise RelationViolationError(f"{path}: table {self.name} columns differ")
        return row

    def render(self) -> str:
        return f"{self.type.declared_name} {self.name} on {self.tbl_name}: {self.sql}"

    @classmethod
    def stored(cls, db) -> tuple[StoredObject, ...]:
        return tuple(cls.read(db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND type != 'trigger' AND name NOT LIKE 'sqlite_%' ORDER BY type,name")))


def derived(row: type[TypedTable]) -> bool:
    return any(row in kind.schema_rows() for kind in DerivedTables.schema_members())


@dataclass(frozen=True)
class RowCount(TypedRow):
    rows: int

    @classmethod
    def of(cls, db, tables) -> dict[str, int]:
        return {table.name: cls.read(db.execute(f'SELECT COUNT(*) AS rows FROM "{table.name}"'))[0].rows
                for table in tables}


@dataclass(frozen=True)
class FileCarried:
    """What one file's carry changed: the catalog before and after, and the rows kept."""

    path: Path
    before: tuple[StoredObject, ...]
    after: tuple[StoredObject, ...]
    kept: dict[str, int]
    retired: dict[str, int]

    def renamed(self) -> dict[str, str]:
        """Old index name to declared name, matched by what the index covers."""
        after = {unnamed(item.sql): item.name for item in self.after if item.type.named_by_coverage}
        names = {item.name for item in self.after}
        return {item.name: after[unnamed(item.sql)] for item in self.before
                if item.type.named_by_coverage and item.name not in names and unnamed(item.sql) in after}


@dataclass(frozen=True)
class StoredFile:
    """One durable file as its catalog reads, before anything is written."""

    path: Path
    objects: tuple[StoredObject, ...]
    user_version: int

    @classmethod
    def read(cls, path: Path) -> StoredFile:
        with closing(CoordinationStore.read_snapshot(path)) as db:
            (version,) = SQLiteUserVersion.read(db.execute("PRAGMA user_version"))
            return cls(path, StoredObject.stored(db), version.user_version)

    @property
    def rebuilds(self) -> bool:
        """Every table it holds is derived, so its owner rebuilds it on open."""
        tables = [item for item in self.objects if item.type.holds_rows and not item.retired]
        return bool(tables) and all(derived(item.declaration(self.path)) for item in tables)

    @property
    def pending(self) -> bool:
        """Whether its catalog differs from what its tables' declarations derive."""
        rows = [item.row(self.path) for item in self.objects
                if item.type.holds_rows and not item.retired]
        declared = {name: sql for row in rows for name, sql in row.structure_objects().items()}
        return (self.user_version != 0
                or {item.name: item.sql for item in self.objects} != declared)


def stores(root: Path) -> tuple[StoredFile, ...]:
    """Every durable SQLite file the bus declares, and every saved session's input proof
    under it. A step writes only inside the root it is given (practice.py), so the
    registry's absolute paths, which on a practice copy name the live bus, are never read."""
    declared = {path for owner, directory in SQLiteFile.declarations_in(root)
                if issubclass(owner, DurableFile) for path in owner.matching_in(directory)}
    return tuple(map(StoredFile.read, sorted(declared)))


def pending(root: Path) -> tuple[StoredFile, ...]:
    return tuple(store for store in stores(root) if not store.rebuilds and store.pending)


def carry(path: Path) -> FileCarried:
    if not path.is_file():
        raise RelationViolationError(f"{path} does not exist; nothing to carry")
    with closing(CoordinationStore.writer(path, isolation_level=None)) as db:
        with SQLiteFile.write_transaction(db):
            before = StoredObject.stored(db)
            tables = [item for item in before if item.type.holds_rows]
            kept = [item for item in tables if not item.retired]
            rows = tuple(item.row(path) for item in kept)
            declared = {name: sql for row in rows for name, sql in row.structure_objects().items()}
            counts = RowCount.of(db, kept)
            retired = RowCount.of(db, [item for item in tables if item.retired])
            for item in before:
                if not item.type.holds_rows and declared.get(item.name) != item.sql:
                    item.drop(db)
            for item in tables:
                if item.retired:
                    item.drop(db)
            for row in rows:
                for statement in row.structure_objects(if_not_exists=True).values():
                    db.execute(statement)
            stored = [SQLiteSchemaObject(item.name, item.sql) for item in StoredObject.stored(db)]
            if differences := SQLiteSchemaObject.differences(stored, declared):
                raise RelationViolationError(f"{path}: still differs: {differences}")
            if RowCount.of(db, kept) != counts:
                raise RelationViolationError(f"{path}: row counts changed")
            db.execute("PRAGMA user_version=0")
            after = StoredObject.stored(db)
    return FileCarried(path, before, after, counts, retired)


class DeclaredSchema(Carry):
    """Durable files and session input proofs keep every row on their declared schema."""

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return tuple(path for store in pending(root) for path in SQLiteFile.files(store.path)
                     if path.exists())

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        stale = pending(root)
        if not apply:
            return Carried(len(stale))
        carried = tuple(carry(store.path) for store in stale)
        return Carried(len(carried), {
            "renamed": sum(len(item.renamed()) for item in carried),
            "retired_rows": sum(sum(item.retired.values()) for item in carried),
        }, tuple(str(item.path.relative_to(root)) for item in carried))
