"""Bring a stopped store's stored rules up to the current declarations, on a new copy.

A store keeps the table and trigger text it was created with. When a
declaration gains a state, its CHECKs, generated-column expressions and
triggers change. This tool rebuilds a new copy from the current declarations,
preserving stored inputs and recomputing generated columns. It never writes the
source, and refuses changes to columns, keys, indexes or table options. New
declared triggers are installed together with the rebuilt tables.

Run it with every writer of the store stopped; a copy made from a store that
has since advanced must not replace it.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from agent_comms.schema_report import StoreSchemaReport
from agent_comms.typed_table import SQLiteSchemaObject, TypedTable
from agent_comms.owner_cutover import StoppedOwnerInstallation
from agent_comms.owner_restart import OwnerRestartRequest

# What must be identical between the stored table and its current declaration.
_STRUCTURE = ("table_xinfo", "foreign_key_list", "index_list")


def stored_columns(db: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """SQLite owns physical row identity as well as declared stored fields."""
    columns = [row[1] for row in db.execute(f'PRAGMA table_xinfo("{table}")') if row[6] == 0]
    without_rowid, = db.execute(
        "SELECT wr FROM pragma_table_list WHERE schema='main' AND name=?", (table,)).fetchone()
    if not without_rowid:
        # An explicit INTEGER PRIMARY KEY aliases rowid; retaining both is valid.
        columns.insert(0, "_rowid_")
    return tuple(columns)


def rows_digest(db: sqlite3.Connection, table: str) -> str:
    """Hash stored inputs; generated values belong to their SQL declarations."""
    columns = stored_columns(db, table)
    order = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
    digest = hashlib.sha256()
    for row in db.execute(f'SELECT {order} FROM "{table}" ORDER BY {order}'):
        digest.update(repr(tuple(row)).encode() + b"\n")
    return digest.hexdigest()


def _structure(db: sqlite3.Connection, table: str) -> dict[str, list]:
    found = {pragma: db.execute(f'PRAGMA {pragma}("{table}")').fetchall() for pragma in _STRUCTURE}
    found["options"] = db.execute(
        "SELECT wr, strict FROM pragma_table_list WHERE name=?", (table,)).fetchall()
    return found


def _require_same_structure(before: sqlite3.Connection, owner: type[TypedTable]) -> None:
    """Rules may change; stored fields, keys and table options may not."""
    name = owner.declared_name
    with closing(sqlite3.connect(":memory:")) as proof:
        declared = owner.schema_objects()
        proof.execute(declared[name])
        for sql in declared.values():
            if sql.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX")):
                proof.execute(sql)
        if _structure(before, name) != _structure(proof, name):
            raise ValueError(f"{name}: columns, keys or indexes changed")


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
            if change.stored is None and change.declared and change.declared.startswith("CREATE TRIGGER "):
                kinds[change.name] = "trigger"
            elif change.stored is None or change.declared is None:
                raise ValueError(f"{change.name}: object added or removed, not a rule change")
            if kinds[change.name] == "table":
                _require_same_structure(before, TypedTable.decode(change.name))
            elif kinds[change.name] != "trigger":
                raise ValueError(f"{change.name}: {kinds[change.name]} text changed")
        objects = SQLiteSchemaObject.read(before.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"))
        expected = {item.name: item.sql for item in objects} | {
            change.name: change.declared for change in changes}
        # Snapshot hashes cover all stored inputs, including sqlite_sequence.
        tables = [row[0] for row in before.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        digests = {name: rows_digest(before, name) for name in tables}
        with destination.open("xb"):
            pass
        destination.chmod(0o600)
        try:
            with closing(sqlite3.connect(destination)) as copy:
                copy.execute("BEGIN IMMEDIATE")
                for name in tables:
                    if name != "sqlite_sequence":
                        copy.execute(expected[name])
                for name in sorted(tables, key=lambda name: name == "sqlite_sequence"):
                    # Creating AUTOINCREMENT tables creates this table already.
                    if name == "sqlite_sequence":
                        copy.execute("DELETE FROM sqlite_sequence")
                    columns = stored_columns(before, name)
                    selected = ",".join('"' + column.replace('"', '""') + '"' for column in columns)
                    copy.executemany(
                        f'INSERT INTO "{name}" ({selected}) VALUES ({",".join("?" for _ in columns)})',
                        before.execute(f'SELECT {selected} FROM "{name}"'))
                # Historical rows are copied as data, never replayed through triggers.
                for name, sql in expected.items():
                    if kinds[name] != "table":
                        copy.execute(sql)
                for setting in ("user_version", "application_id"):
                    value = before.execute(f"PRAGMA {setting}").fetchone()[0]
                    copy.execute(f"PRAGMA {setting}={value}")
                actual = SQLiteSchemaObject.read(copy.execute(
                    "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"))
                if SQLiteSchemaObject.differences(actual, expected):
                    raise ValueError("copy changed another schema object")
                if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("copy integrity check failed")
                if copy.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("copy foreign-key check failed")
                if {name: rows_digest(copy, name) for name in tables} != digests:
                    raise ValueError("copy stored row values changed")
                if StoreSchemaReport.read(copy, destination).differences:
                    raise ValueError("copy still differs from current declarations")
                copy.commit()
        except BaseException:
            destination.unlink()
            raise
    return {"source": str(source), "copy": str(destination),
            "carried": [change.name for change in changes], "rows_preserved": digests}


class InstallDeclaredRules(StoppedOwnerInstallation):
    """One-use installation; the existing batch owns stopping and restarting."""

    def __init__(self, backup: Path):
        self.backup = backup

    def restart(self, lifecycle, request):
        with ExitStack() as self.resources:
            super().restart(lifecycle, request)
            return self.recover_and_launch()

    def require_selection(self, snapshot, owners) -> None:
        if {owner.name for owner in owners} != {
            owner.name for owner in OwnerRestartRequest().threads(snapshot)
        }:
            raise ValueError("Schema installation requires every managed native owner")
        if self.backup.exists():
            raise FileExistsError(self.backup)

    def failed(self, failure):
        self.leave_stopped(failure)

    def require_launch(self, lifecycle) -> None:
        lifecycle.require_launch_authority()

    def after_stopped(self, lifecycle) -> None:
        from agent_comms.active_route import active_route_path, read_active_route
        from publish_openhcs_recovery import ROOT, require_no_clients
        from publish_runtime import RuntimePublication

        if lifecycle.root != ROOT:
            raise ValueError("The reviewed client exclusion names a different root")
        # Worker observers borrow the route too. Stop them before taking the
        # exclusive lock; retain it only for replacement and recovery.
        directory = os.open(active_route_path().parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.resources.callback(os.close, directory)
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if read_active_route().root != lifecycle.root:
            raise ValueError("Published route differs from the migration root")
        require_no_clients(())
        source = lifecycle.root / "coordination.sqlite3"
        replacement = source.with_name("coordination.rebuilt.sqlite3")
        # SQLite, including any committed journal pages, supplies the backup.
        with self.backup.open("xb"):
            pass
        self.backup.chmod(0o600)
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(self.backup)) as saved:
                original.backup(saved)
        self.result = carry(self.backup, replacement)
        require_no_clients(())
        # No process may retain the old inode or its journal across replacement.
        for suffix in ("-wal", "-shm", "-journal"):
            if source.with_name(source.name + suffix).exists():
                raise ValueError("Stopped coordinator still has a journal: " + suffix)
        for path in (self.backup, replacement):
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        os.replace(replacement, source)
        directory = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        RuntimePublication.installed().publish()

    def complete(self, stopped):
        self.after_stopped(stopped.lifecycle)
        # Recovery takes the canonical wire/bus/registry boundary itself. Finish
        # this exclusive wire scope first; retain the original launch records in
        # memory. The handoff revalidates every stopped owner before any launch.
        self.stopped = stopped
        return ()

    def recover_and_launch(self):
        from agent_comms.attempt_recovery import RecoveryMonitorCapability

        stopped = self.stopped
        snapshot = stopped.lifecycle.registry.snapshot()
        for owner in stopped.handoff.owners:
            thread = owner.require_current(snapshot)
            RecoveryMonitorCapability.settle_stopped_owner(stopped.lifecycle.root, thread)
        with stopped.lifecycle.restart_wire():
            # The new runtime is published and recovery is complete. Its new
            # observers must be able to borrow the route during startup.
            self.resources.close()
            return stopped.handoff.launch(stopped.lifecycle)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--copy", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(carry(args.source, args.copy), indent=2))
