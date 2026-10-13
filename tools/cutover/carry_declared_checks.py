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
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3

from agent_comms.field_codec import FieldCodec
from agent_comms.schema_report import StoreSchemaReport
from agent_comms.typed_table import SQLiteSchemaObject, TypedTable


@dataclass(frozen=True)
class OriginalDeclaration:
    objects: dict[str, str]
    constraints: dict[str, dict[str, tuple[str, ...]]]

    def require_check_only(self, owner: type[TypedTable]) -> None:
        name = owner.declared_name
        old = self.objects[name]
        new = owner.schema_objects()[name]
        fields = owner._fields()
        declared = self.constraints[name]
        if set(declared) != {item.name for item in fields}:
            raise ValueError(f"{name}: declared fields changed, not CHECK-only")
        for item in fields:
            previous = declared[item.name]
            current = item.storage.constraints(item.name, item.annotation)
            if previous == current:
                continue
            # Remove only the changed generated field constraints, each obtained
            # from its original/current storage declaration. Explicit column and
            # table CHECKs remain in the comparison, as does every other DDL byte.
            for expression in previous:
                clause = f" CHECK ({expression})"
                if clause not in old:
                    raise ValueError(f"{name}.{item.name}: original CHECK is absent")
                old = old.replace(clause, "", 1)
            for expression in current:
                clause = f" CHECK ({expression})"
                if clause not in new:
                    raise ValueError(f"{name}.{item.name}: current CHECK is absent")
                new = new.replace(clause, "", 1)
        if old != new:
            raise ValueError(f"{name}: unrelated DDL difference, not CHECK-only")


def rows_digest(db: sqlite3.Connection, table: str) -> str:
    columns = [row[1] for row in db.execute(f'PRAGMA table_xinfo("{table}")')]
    order = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
    digest = hashlib.sha256()
    for row in db.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
        digest.update(repr(tuple(row)).encode() + b"\n")
    return digest.hexdigest()


def carry(source: Path, destination: Path, original: OriginalDeclaration) -> dict:
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
        if change.stored != original.objects[change.name] or change.declared != owner.schema_objects()[change.name]:
            raise ValueError(f"{change.name}: not the authentic original table declaration")
        original.require_check_only(owner)
        owners.append(owner)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as before:
        before.execute("BEGIN")
        objects = SQLiteSchemaObject.read(before.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"))
        preserved_objects = {item.name: item.sql for item in objects}
        for change in changes:
            if preserved_objects.get(change.name) != change.stored:
                raise ValueError(f"{change.name}: source schema advanced before snapshot")
        foreign_keys = {owner.declared_name: before.execute(
            f'PRAGMA foreign_key_list("{owner.declared_name}")').fetchall() for owner in owners}
        for owner in owners:
            # Reject structural changes. Validate every stored row against the
            # current declaration, including generated columns, with no triggers.
            with closing(sqlite3.connect(":memory:")) as proof:
                proof.execute(owner.schema_objects()[owner.declared_name])
                old_columns = before.execute(f'PRAGMA table_xinfo("{owner.declared_name}")').fetchall()
                if old_columns != proof.execute(f'PRAGMA table_xinfo("{owner.declared_name}")').fetchall():
                    raise ValueError(f"{owner.declared_name}: column layout changed")
                if foreign_keys[owner.declared_name] != proof.execute(
                        f'PRAGMA foreign_key_list("{owner.declared_name}")').fetchall():
                    raise ValueError(f"{owner.declared_name}: foreign-key declaration changed")
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
        expected_objects = preserved_objects | {change.name: change.declared for change in changes}
        actual = SQLiteSchemaObject.read(accepted.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"))
        if SQLiteSchemaObject.differences(actual, expected_objects):
            raise ValueError("copy changed another schema object")
        for owner in owners:
            if foreign_keys[owner.declared_name] != accepted.execute(
                    f'PRAGMA foreign_key_list("{owner.declared_name}")').fetchall():
                raise ValueError(f"{owner.declared_name}: copy foreign-key declaration changed")
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
    parser.add_argument("--original-declaration", type=Path, required=True)
    args = parser.parse_args()
    original = FieldCodec.decode(OriginalDeclaration, json.loads(args.original_declaration.read_text()))
    print(json.dumps(carry(args.source, args.copy, original), indent=2))
