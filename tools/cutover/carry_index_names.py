"""Reconcile declared index identities across bus stores, never table data."""

import sqlite3
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.locked_store import RootFile
from agent_comms.schema_report import StoreSchemaReport
from agent_comms.typed_table import SQLiteSchemaDifference, SQLiteSchemaObject, TypedStore, _identifier

from .carry import Carried, Carry


class IndexNames(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        TypedStore.declarations()  # Acquire the declared modules, not a filename/version roster.
        return tuple(sorted(path for path in RootFile.paths_in(root)
                            if path.is_file() and CoordinationStore.is_database(path)))

    @staticmethod
    def changes(
        path: Path, db: sqlite3.Connection, stores: dict[Path, type[TypedStore]],
    ) -> tuple[SQLiteSchemaDifference, ...]:
        if owner := stores.get(path):
            report = StoreSchemaReport.for_store(db, path, owner)
        else:
            report = StoreSchemaReport.read(db, path)
        stored_indexes = {item.name for item in SQLiteSchemaObject.read(db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%'"))}
        if report.undeclared_tables or any(
            (change.stored is not None and change.name not in stored_indexes)
            or (change.declared is not None and change.name not in report.index_names)
            for change in report.differences
        ):
            raise ValueError(f"{path.name} differs outside its declared indexes")
        return report.differences

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        # Preflight every file before changing any store. Schema writers are
        # stopped and the caller holds the wire lock, as for every carry step.
        stores = {store.path_in(root): store for store in TypedStore.declarations()}
        planned = []
        for path in cls.backup_files(root):
            with CoordinationStore.observing(path) as db:
                changes = cls.changes(path, db, stores)
            if changes:
                planned.append((path, changes))
        changed = 0
        rewritten = []
        for path, changes in planned:
            if apply:
                with closing(CoordinationStore.writer(path)) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    changes = cls.changes(path, db, stores)
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
