"""Rewrite declared mutation guards once, retaining every application row.

SnapshotMeta is the sole exception: its existing schema digest changes in the same
transaction as its guards, after the old digest has been checked against the stored
schema. No runtime reader accepts both schemas. Retire after installation.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import closing, nullcontext
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import DurableFile, SQLiteFile
from agent_comms.private_sidecar import SnapshotMeta, _digest
from agent_comms.typed_table import SQLiteSchemaObject, SQLiteTableName, TableSchema, TypedTable


class TableTriggers(Carry):
    """One install writer for stored triggers and their sidecar schema digest."""

    @classmethod
    def paths(cls, root: Path) -> tuple[Path, ...]:
        # Explicit offline discovery; ordinary TypedTable reads only imported members.
        tuple(TypedTable.__registry__.values())
        paths = {
            path for owner, directory in SQLiteFile.declarations_in(root)
            if issubclass(owner, DurableFile) for path in owner.matching_in(directory)
        }
        for path in paths:
            cls.require_confined(root, path)
        return tuple(sorted(paths))

    @classmethod
    def rewrite(cls, path: Path, *, apply: bool) -> int:
        opener = CoordinationStore.writer if apply else CoordinationStore.read_snapshot
        with closing(opener(path)) as db:
            transaction = SQLiteFile.write_transaction(db) if apply else nullcontext(db)
            with transaction:
                rows = tuple(TypedTable.member_named(table.name)
                             for table in SQLiteTableName.stored(db))
                declared = {name: sql for row in rows for name, sql in row.schema_objects().items()}
                structure = {name: sql for row in rows for name, sql in row.structure_objects().items()}
                stored = SQLiteSchemaObject.read(db.execute(
                    "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY rowid"))
                actual_guards = SQLiteSchemaObject.read(db.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY rowid"))
                guard_names = {guard.name for guard in actual_guards}
                actual_structure = [item for item in stored if item.name not in guard_names]
                if differences := SQLiteSchemaObject.differences(actual_structure, structure):
                    raise RelationViolationError(f"{path}: table/index/view differs: {differences}")
                expected = {name: sql for name, sql in declared.items() if name not in structure}
                digest_changed = False
                digest = ""
                if SnapshotMeta in rows:
                    (row,) = (row for row in rows if row is not SnapshotMeta)
                    (meta,) = SnapshotMeta.select(db)
                    # Sidecar digests use declaration insertion order, which is also
                    # the catalog's creation order before this first trigger carry.
                    old_schema = {item.name: item.sql for item in stored}
                    old_digest = hashlib.sha256(json.dumps(
                        old_schema, separators=(",", ":")).encode()).hexdigest()
                    digest = _digest(row)
                    complete = not SQLiteSchemaObject.differences(stored, declared)
                    if meta.ddl_digest != old_digest and not (meta.ddl_digest == digest and complete):
                        raise RelationViolationError(f"{path}: sidecar schema digest is unknown")
                    digest_changed = meta.ddl_digest != digest
                differences = SQLiteSchemaObject.differences(actual_guards, expected)
                if not differences and not digest_changed:
                    return 0
                if not apply:
                    return 1
                remaining = {guard.name: guard.sql for guard in actual_guards}
                metadata_guards = SnapshotMeta.triggers() if digest_changed else {}
                for guard in actual_guards:
                    if expected.get(guard.name) != guard.sql or guard.name in metadata_guards:
                        db.execute('DROP TRIGGER "' + guard.name.replace('"', '""') + '"')
                        del remaining[guard.name]
                if digest_changed:
                    SnapshotMeta.update(db, where="singleton=1", ddl_digest=digest)
                for name, sql in expected.items():
                    if remaining.get(name) != sql:
                        db.execute(sql)
                if differences := SQLiteSchemaObject.differences(
                    TableSchema.stored_schema(db), declared
                ):
                    raise RelationViolationError(f"{path}: schema still differs: {differences}")
                return 1

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return tuple(member for path in cls.paths(root) if cls.rewrite(path, apply=False)
                     for member in SQLiteFile.files(path) if member.exists())

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        changed = tuple(path for path in cls.paths(root) if cls.rewrite(path, apply=apply))
        return Carried(len(changed), {"files": len(changed)},
                       tuple(str(path.relative_to(root)) for path in changed) if apply else ())
