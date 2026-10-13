"""Bring a stopped store's stored rules up to the current declarations, on a new copy.

A store keeps the table and trigger text it was created with. When a
declaration gains a state, its CHECKs, generated-column expressions and
triggers change while the columns and rows do not. This tool writes a new copy
whose stored text is the current declaration and proves that every existing row
is unchanged and valid under it. It never writes the source, and refuses any
change to columns, keys, indexes or the set of objects.

Run it with every writer of the store stopped; a copy made from a store that
has since advanced must not replace it.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from agent_comms.schema_report import StoreSchemaReport
from agent_comms.typed_table import SQLiteSchemaObject, TypedTable

# What must be identical between the stored table and its current declaration.
_STRUCTURE = ("table_xinfo", "foreign_key_list", "index_list")


def rows_digest(db: sqlite3.Connection, table: str) -> str:
    columns = [row[1] for row in db.execute(f'PRAGMA table_xinfo("{table}")')]
    order = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
    digest = hashlib.sha256()
    for row in db.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
        digest.update(repr(tuple(row)).encode() + b"\n")
    return digest.hexdigest()


def _structure(db: sqlite3.Connection, table: str) -> dict[str, list]:
    found = {pragma: db.execute(f'PRAGMA {pragma}("{table}")').fetchall() for pragma in _STRUCTURE}
    found["options"] = db.execute(
        "SELECT wr, strict FROM pragma_table_list WHERE name=?", (table,)).fetchall()
    return found


def _require_same_rows(before: sqlite3.Connection, owner: type[TypedTable]) -> None:
    """Every stored row, written again under the current declaration, is the same row."""
    name = owner.declared_name
    with closing(sqlite3.connect(":memory:")) as proof:
        declared = owner.schema_objects()
        proof.execute(declared[name])
        for sql in declared.values():
            if sql.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX")):
                proof.execute(sql)
        if _structure(before, name) != _structure(proof, name):
            raise ValueError(f"{name}: columns, keys or indexes changed")
        writable = tuple(item.name for item in owner._fields() if item.column.generated is None)
        columns = owner._column_list(writable)
        proof.executemany(
            f'INSERT INTO "{name}" ({columns}) VALUES ({",".join("?" for _ in writable)})',
            before.execute(f'SELECT {columns} FROM "{name}"'))
        if rows_digest(before, name) != rows_digest(proof, name):
            raise ValueError(f"{name}: generated row values changed")


def carry(source: Path, destination: Path) -> dict:
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as before:
        before.execute("PRAGMA query_only=ON")
        before.execute("BEGIN")
        observed = StoreSchemaReport.read(before, source)
        if observed.undeclared_tables:
            raise ValueError(f"undeclared tables cannot be carried: {observed.undeclared_tables}")
        changes = observed.differences
        if not changes:
            raise ValueError("store already matches its declarations")
        kinds = dict(before.execute("SELECT name,type FROM sqlite_master WHERE sql IS NOT NULL"))
        for change in changes:
            if change.stored is None or change.declared is None:
                raise ValueError(f"{change.name}: object added or removed, not a rule change")
            if kinds[change.name] == "table":
                _require_same_rows(before, TypedTable.decode(change.name))
            elif kinds[change.name] != "trigger":
                raise ValueError(f"{change.name}: {kinds[change.name]} text changed")
        objects = SQLiteSchemaObject.read(before.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"))
        expected = {item.name: item.sql for item in objects} | {
            change.name: change.declared for change in changes}
        # Snapshot hashes also cover untouched tables and sqlite_sequence.
        tables = [row[0] for row in before.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        digests = {name: rows_digest(before, name) for name in tables}
        with destination.open("xb"):
            pass
        destination.chmod(0o600)
        with closing(sqlite3.connect(destination, isolation_level=None)) as copy:
            before.backup(copy)
            copy.execute("BEGIN IMMEDIATE")
            copy.execute("PRAGMA writable_schema=ON")
            for change in changes:
                copy.execute("UPDATE sqlite_master SET sql=? WHERE name=? AND type=?",
                             (change.declared, change.name, kinds[change.name]))
            version = copy.execute("PRAGMA schema_version").fetchone()[0]
            copy.execute(f"PRAGMA schema_version={version + 1}")
            copy.execute("PRAGMA writable_schema=OFF")
            copy.execute("COMMIT")
    with closing(sqlite3.connect(destination)) as accepted:
        actual = SQLiteSchemaObject.read(accepted.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"))
        if SQLiteSchemaObject.differences(actual, expected):
            raise ValueError("copy changed another schema object")
        if accepted.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("copy integrity check failed")
        if accepted.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("copy foreign-key check failed")
        if {name: rows_digest(accepted, name) for name in tables} != digests:
            raise ValueError("copy row values changed")
    if StoreSchemaReport.observe(destination).differences:
        raise ValueError("copy still differs from current declarations")
    return {"source": str(source), "copy": str(destination),
            "carried": [change.name for change in changes], "rows_preserved": digests}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--copy", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(carry(args.source, args.copy), indent=2))
