"""Read existing SQLite declarations without opening a writer or repairing state."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from .coordination_database import CoordinationStore
from .native_session_files import NativeSessionFiles
from .typed_table import SQLiteSchemaDifference, SQLiteSchemaObject, TypedRow, TypedTable


@dataclass(frozen=True)
class _StoredTable(TypedRow):
    name: str


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
        paths.update(NativeSessionFiles(Path(thread.session_file)).input_proof
                     for thread in comms.registry.all_threads().values()
                     if thread.session_file)
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
        actual = SQLiteSchemaObject.read(db.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"))
        tables = _StoredTable.read(db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
        expected = {}
        compared = set()
        unknown = []
        for table in tables:
            try:
                owner = TypedTable.decode(table.name)
            except ValueError:
                unknown.append(table.name)
                continue
            declarations = owner.schema_objects()
            expected.update(declarations)
            compared.update(declarations)
            # Extra triggers or indexes attached to a known table also differ.
            compared.update(row.name for row in _StoredTable.read(db.execute(
                "SELECT name FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL",
                (table.name,))))
        changes = SQLiteSchemaObject.differences(
            [item for item in actual if item.name in compared], expected)
        return cls(str(path), changes, tuple(sorted(unknown)))


@dataclass(frozen=True)
class UnavailableStoreSchemaReport:
    path: str
    error: str
