"""Reconcile declared index identities in every present TypedStore, never table data."""

import sqlite3
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.typed_table import (
    SQLiteSchemaDifference,
    SQLiteSchemaObject,
    TypedStore,
    _identifier,
)

from .carry import Carried, Carry


class IndexNames(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return tuple(store.path_in(root) for store in TypedStore.declarations()
                     if store.path_in(root).is_file())

    @staticmethod
    def changes(store: type[TypedStore], db: sqlite3.Connection) -> tuple[SQLiteSchemaDifference, ...]:
        changes = SQLiteSchemaObject.differences(TypedStore._schema(db), store.declared_schema())
        stored_indexes = {item.name for item in SQLiteSchemaObject.read(db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%'"))}
        declared_indexes = store.declared_indexes()
        if any((change.stored is not None and change.name not in stored_indexes)
               or (change.declared is not None and change.name not in declared_indexes)
               for change in changes):
            raise ValueError(f"{store.filename} differs outside its declared indexes")
        return changes

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        # Preflight the complete set before changing any store. Schema writers are
        # stopped and the caller holds the wire lock, as for every carry step.
        planned = []
        for store in TypedStore.declarations():
            path = store.path_in(root)
            if path.is_file():
                with CoordinationStore.observing(path) as db:
                    changes = cls.changes(store, db)
                if changes:
                    planned.append((store, path, changes))
        changed = 0
        rewritten = []
        for store, path, changes in planned:
            if apply:
                with closing(CoordinationStore.writer(path)) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    changes = cls.changes(store, db)
                    for change in changes:
                        if change.stored is not None:
                            db.execute(f"DROP INDEX {_identifier(change.name)}")
                        if change.declared is not None:
                            db.execute(change.declared)
                if changes:
                    rewritten.append(str(path.relative_to(root)))
            changed += len(changes)
        return Carried(changed, {"stores": len(planned), "indexes": changed}, tuple(rewritten))


if __name__ == "__main__":
    IndexNames.main()
