"""Read existing SQLite declarations without opening a writer or repairing state."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .cli_result import CliResult
from .coordination_database import CoordinationStore
from .declared_family import UnknownFamilyMember
from .field_codec import FieldCodec
from .typed_table import SQLiteSchemaDifference, SQLiteSchemaObject, SQLiteTableName, TypedTable


@dataclass(frozen=True)
class StoreSchemaReport:
    path: str
    differences: tuple[SQLiteSchemaDifference, ...]
    undeclared_tables: tuple[str, ...]

    @classmethod
    def scan(cls, comms) -> tuple[StoreSchemaReport | UnavailableStoreSchemaReport, ...]:
        """Inspect existing files and registered journals, not store writers.

        Includes retained copies. Missing databases/tables cannot be inferred
        from another database's rows.
        """
        paths = set(comms.root.rglob("*.sqlite3"))
        paths.update(path
                     for thread in comms.registry.all_threads().values()
                     for files in thread.runtime.storage_files()
                     for path in files.schema_paths)
        reports = []
        for path in sorted(paths):
            if not path.is_file():
                continue
            try:
                reports.append(cls.observe(path))
            except sqlite3.Error as error:
                reports.append(UnavailableStoreSchemaReport(str(path), str(error)))
        return tuple(reports)

    @classmethod
    def observe(cls, path: Path) -> StoreSchemaReport:
        with closing(CoordinationStore.read_snapshot(path)) as db:
            return cls.read(db, path)

    @classmethod
    def read(cls, db: sqlite3.Connection, path: Path) -> StoreSchemaReport:
        """Decode the caller's already-open snapshot; never acquire another one."""
        expected: dict[str, str] = {}
        stored: list[SQLiteSchemaObject] = []
        unknown = []
        for table in SQLiteTableName.stored(db):
            try:
                owner = TypedTable.member_named(table.name)
            except UnknownFamilyMember:
                unknown.append(table.name)
                continue
            expected.update(owner.schema_objects())
            # Extra triggers or indexes attached to a known table also differ.
            stored.extend(owner.stored_objects(db))
        changes = SQLiteSchemaObject.differences(stored, expected)
        return cls(str(path), changes, tuple(sorted(unknown)))


@dataclass(frozen=True)
class UnavailableStoreSchemaReport:
    path: str
    error: str


@dataclass(frozen=True)
class StoreSchemaReports(CliResult):
    """Every store scanned at one read, available or not."""
    reports: tuple[StoreSchemaReport | UnavailableStoreSchemaReport, ...]

    def to_wire(self) -> object:
        return FieldCodec.encode(self.reports)
