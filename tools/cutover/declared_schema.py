"""Carry durable SQLite files onto their declared schema once; keep every row.

Files written before index names were derived from what each index covers hold
positional names (delivery_0_idx), and files written before every file kind was
compared with its declaration hold a sealed copy of it: a meta table with a
digest or version, triggers freezing it, and PRAGMA user_version. Each file is
converted in place inside one write transaction: indexes, triggers and views
come from the declarations of the tables it holds, the sealed copies go, and
every table keeps its rows. Delete once every machine's durable files are carried.
"""

from __future__ import annotations

import argparse
import re
import sys
from contextlib import closing
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from pkgutil import walk_packages

import agent_comms
from agent_comms.coordination_database import CoordinationStore
from agent_comms.declared_family import UnknownFamilyMember
from agent_comms.errors import RelationViolationError
from agent_comms.locked_store import SQLiteFile
from agent_comms.typed_table import DerivedTables, SQLiteSchemaObject, TypedRow, TypedTable

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


def _subclasses(family: type) -> list[type]:
    return [kind for child in family.__subclasses__() for kind in (child, *_subclasses(child))]


_INDEX_NAME = re.compile(r'^(CREATE (?:UNIQUE )?INDEX )(?:"(?:[^"]|"")+"|\S+)( ON )')


def unnamed(sql: str) -> str:
    """An index's SQL without its name: what it covers."""
    return _INDEX_NAME.sub(r"\1\2", sql, count=1)


@dataclass(frozen=True)
class StoredObject(TypedRow):
    """One catalog entry: SQLite's own type, name and table."""

    type: str
    name: str
    tbl_name: str
    sql: str

    @property
    def retired(self) -> bool:
        return self.type == "table" and self.name in RETIRED_TABLES

    def drop(self, db) -> None:
        db.execute(f'DROP {self.type.upper()} IF EXISTS "{self.name}"')

    def row(self, path: Path) -> type[TypedTable]:
        """The declaration of this stored table, refusing one the carry cannot keep."""
        try:
            row = TypedTable.member_named(self.name)
        except UnknownFamilyMember:
            raise RelationViolationError(f"{path}: undeclared table {self.name}") from None
        if any(row in kind.schema_rows() for kind in _subclasses(DerivedTables)):
            raise RelationViolationError(f"{path}: {self.name} is derived; it rebuilds, never carried")
        if self.sql != row.schema_objects()[row.declared_name]:
            raise RelationViolationError(f"{path}: table {self.name} columns differ")
        return row

    def render(self) -> str:
        return f"{self.type} {self.name} on {self.tbl_name}: {self.sql}"

    @classmethod
    def stored(cls, db) -> tuple[StoredObject, ...]:
        return tuple(cls.read(db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name")))


@dataclass(frozen=True)
class RowCount(TypedRow):
    rows: int

    @classmethod
    def of(cls, db, tables) -> dict[str, int]:
        return {table.name: cls.read(db.execute(f'SELECT COUNT(*) AS rows FROM "{table.name}"'))[0].rows
                for table in tables}


@dataclass(frozen=True)
class Carried:
    """What one carry changed: the catalog before and after, and the rows kept."""

    path: Path
    before: tuple[StoredObject, ...]
    after: tuple[StoredObject, ...]
    kept: dict[str, int]
    retired: dict[str, int]

    def renamed(self) -> dict[str, str]:
        """Old index name to declared name, matched by what the index covers."""
        after = {unnamed(item.sql): item.name for item in self.after if item.type == "index"}
        names = {item.name for item in self.after}
        return {item.name: after[unnamed(item.sql)] for item in self.before
                if item.type == "index" and item.name not in names and unnamed(item.sql) in after}

    def render(self) -> str:
        before = {(item.name, item.sql) for item in self.before}
        after = {(item.name, item.sql) for item in self.after}
        renamed = self.renamed()
        lines = [f"{self.path}:"]
        lines += [f"  renamed {old} -> {new}" for old, new in sorted(renamed.items())]
        lines += [f"  dropped {item.render()}" for item in self.before
                  if (item.name, item.sql) not in after and item.name not in renamed]
        lines += [f"  created {item.render()}" for item in self.after
                  if (item.name, item.sql) not in before and item.name not in renamed.values()]
        lines += [f"  rows kept {table}: {rows}" for table, rows in sorted(self.kept.items())]
        lines += [f"  rows retired {table}: {rows}" for table, rows in sorted(self.retired.items())]
        return "\n".join(lines)


def carry(path: Path) -> Carried:
    if not path.is_file():
        raise RelationViolationError(f"{path} does not exist; nothing to carry")
    with closing(CoordinationStore.writer(path, isolation_level=None)) as db:
        with SQLiteFile.write_transaction(db):
            before = StoredObject.stored(db)
            tables = [item for item in before if item.type == "table"]
            kept = [item for item in tables if not item.retired]
            rows = tuple(item.row(path) for item in kept)
            declared = {name: sql for row in rows for name, sql in row.schema_objects().items()}
            counts = RowCount.of(db, kept)
            retired = RowCount.of(db, [item for item in tables if item.retired])
            for item in before:
                if item.type != "table" and declared.get(item.name) != item.sql:
                    item.drop(db)
            for item in tables:
                if item.retired:
                    item.drop(db)
            for row in rows:
                row.create(db, if_not_exists=True)
            if differences := SQLiteSchemaObject.differences(TypedTable.objects_of(db, rows), declared):
                raise RelationViolationError(f"{path}: still differs: {differences}")
            if RowCount.of(db, kept) != counts:
                raise RelationViolationError(f"{path}: row counts changed")
            db.execute("PRAGMA user_version=0")
            after = StoredObject.stored(db)
    return Carried(path, before, after, counts, retired)


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path, help="durable SQLite files")
    for path in parser.parse_args(argv).files:
        print(carry(path).render())


if __name__ == "__main__":
    main(sys.argv[1:])
