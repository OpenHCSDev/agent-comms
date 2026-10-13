"""One-use #762 CHECK carry. Transform a new copy, never the source store.

The old schema JSON must be exported by the original writer's declarations.
Only changed table DDL is accepted; columns, indexes, triggers and all row values
stay unchanged. Coach repeats the carry from a fresh stopped snapshot; these
verification copies must never replace a live store that has since advanced.
Delete this tool after that carry is accepted.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from agent_comms.field_codec import FieldCodec
from agent_comms.schema_report import StoreSchemaReport
from agent_comms.typed_table import TypedTable


def rows_digest(db: sqlite3.Connection, table: str) -> str:
    columns = [row[1] for row in db.execute(f'PRAGMA table_xinfo("{table}")')]
    order = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
    digest = hashlib.sha256()
    for row in db.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
        digest.update(repr(tuple(row)).encode() + b"\n")
    return digest.hexdigest()


def carry(source: Path, destination: Path, original: dict[str, str]) -> dict:
    # The destination is always newly created. A failed copy remains evidence,
    # never an automatically retried operation or an installed replacement.
    observed = StoreSchemaReport.observe(source)
    if observed.undeclared_tables:
        raise ValueError(f"undeclared tables cannot be carried: {observed.undeclared_tables}")
    changes = observed.differences
    if not changes:
        raise ValueError("store already matches; no CHECK carry is needed")
    owners = []
    for change in changes:
        owner = TypedTable.decode(change.name)
        if change.stored != original[change.name] or change.declared != owner.schema_objects()[change.name]:
            raise ValueError(f"{change.name}: not the authentic original table declaration")
        owners.append(owner)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as before:
        before.execute("BEGIN")
        for owner in owners:
            # Reject structural changes. Validate every stored row against the
            # current declaration, including generated columns, with no triggers.
            with closing(sqlite3.connect(":memory:")) as proof:
                proof.execute(owner.schema_objects()[owner.declared_name])
                old_columns = before.execute(f'PRAGMA table_xinfo("{owner.declared_name}")').fetchall()
                if old_columns != proof.execute(f'PRAGMA table_xinfo("{owner.declared_name}")').fetchall():
                    raise ValueError(f"{owner.declared_name}: column layout changed")
                writable = tuple(item.name for item in owner._fields() if item.column.generated is None)
                columns = owner._column_list(writable)
                sql = f'INSERT INTO "{owner.declared_name}" ({columns}) VALUES ({",".join("?" for _ in writable)})'
                proof.executemany(sql, before.execute(f'SELECT {columns} FROM "{owner.declared_name}"'))
                if rows_digest(before, owner.declared_name) != rows_digest(proof, owner.declared_name):
                    raise ValueError(f"{owner.declared_name}: generated row values changed")
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
                copy.execute("UPDATE sqlite_master SET sql=? WHERE name=? AND type='table'",
                             (change.declared, change.name))
            version = copy.execute("PRAGMA schema_version").fetchone()[0]
            copy.execute(f"PRAGMA schema_version={version + 1}")
            copy.execute("PRAGMA writable_schema=OFF")
            copy.execute("COMMIT")
    with closing(sqlite3.connect(destination)) as accepted:
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
    parser.add_argument("--original-schema", type=Path, required=True)
    args = parser.parse_args()
    original = FieldCodec.decode(dict[str, str], json.loads(args.original_schema.read_text()))
    print(json.dumps(carry(args.source, args.copy, original), indent=2))
