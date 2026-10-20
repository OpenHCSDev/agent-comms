"""Reconcile declared index identities across bus stores, never table data."""

import sqlite3
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.locked_store import RootFile
from agent_comms.schema_report import StoreSchemaReport
from agent_comms.typed_table import SQLiteSchemaObject, TypedStore, _identifier

from .carry import Carried, Carry


class IndexNames(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        TypedStore.declarations()  # Acquire modules, not a filename/version roster.
        return tuple(
            sorted(
                path
                for path in RootFile.paths_in(root)
                if path.is_file() and CoordinationStore.is_database(path)
            )
        )

    @staticmethod
    def changes(
        path: Path,
        db: sqlite3.Connection,
        stores: dict[Path, type[TypedStore]],
    ) -> tuple[StoreSchemaReport, frozenset[str]]:
        report = (
            StoreSchemaReport.for_store(db, path, owner)
            if (owner := stores.get(path))
            else StoreSchemaReport.read(db, path)
        )
        stored_indexes = frozenset(
            item.name
            for item in SQLiteSchemaObject.read(
                db.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            )
        )
        stored_names = {item.name for item in TypedStore._schema(db)}
        non_indexes = (stored_names - stored_indexes).union(report.non_index_names)
        if report.undeclared_tables or any(
            change.name in non_indexes for change in report.differences
        ):
            raise ValueError(f"{path.name} differs outside its declared indexes")
        return report, stored_indexes

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        # Preflight every file before changing any store. Schema writers are
        # stopped and the caller holds the wire lock, as for every carry step.
        stores = {store.path_in(root): store for store in TypedStore.declarations()}
        planned = []
        for path in cls.backup_files(root):
            with CoordinationStore.observing(path) as db:
                report, stored_indexes = cls.changes(path, db, stores)
            if report.differences:
                planned.append((path, report, stored_indexes))
        changed = 0
        rewritten = []
        for path, report, stored_indexes in planned:
            if apply:
                with closing(CoordinationStore.writer(path)) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    report, stored_indexes = cls.changes(path, db, stores)
                    for change in report.differences:
                        if change.name in stored_indexes:
                            db.execute(f"DROP INDEX {_identifier(change.name)}")
                        if change.name in report.index_sql:
                            db.execute(report.index_sql[change.name])
                if report.differences:
                    rewritten.append(str(path.relative_to(root)))
            changed += len(report.differences)
        return Carried(changed, {"stores": len(planned), "indexes": changed}, tuple(rewritten))


if __name__ == "__main__":
    IndexNames.main()
