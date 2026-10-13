"""Read existing SQLite declarations without opening a writer or repairing state."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3

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
    def observe(cls, path: Path) -> StoreSchemaReport:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=1, isolation_level=None)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
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
                compared.update(row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL",
                    (table.name,)))
            changes = SQLiteSchemaObject.differences(
                [item for item in actual if item.name in compared], expected)
            return cls(str(path), changes, tuple(sorted(unknown)))


@dataclass(frozen=True)
class UnavailableStoreSchemaReport:
    path: str
    error: str


def report_stores(comms) -> tuple[StoreSchemaReport | UnavailableStoreSchemaReport, ...]:
    """Inspect existing files, including retained copies, never instantiate stores.

    Missing databases/tables cannot be inferred from another database's rows.
    Native journals use the existing registered session's file declaration.
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
            reports.append(StoreSchemaReport.observe(path))
        except sqlite3.Error as error:
            reports.append(UnavailableStoreSchemaReport(str(path), str(error)))
    return tuple(reports)
