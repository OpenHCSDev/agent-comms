"""The installer's reconcile, run the way the installer runs it: the target's CLI under the
root's maintenance hold, on a bus whose stored schemas differ. Every file it rewrites is
named, so the backup and the rehearsal account for it like a carry's rewrite; nothing the
CLI opens first reconciles a store behind the verb's back. Without the hold, a schema that
differs is refused, naming the store, and the file stays as it was."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import pytest

from agent_comms.locked_store import LiveFilesRequest
from agent_comms.activity import ActivityStore
from agent_comms.bus import Bus
from agent_comms.cli_commands import (
    CliCommand,
    PlannedReconcileCliCommand,
    ReadBackCliCommand,
    ReconcileStoresCliCommand,
)
from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordinator import Coordination
from agent_comms.installer.backup import bus_files
from agent_comms.locked_store import SQLiteFile
from agent_comms.maintenance_barrier import MaintenanceHolder, NoMaintenanceHolder
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.private_runtime_schema import PrivateCoordinatorSchema
from agent_comms.triage_native_sources import TriageNativeSource
from agent_comms.typed_table import (
    Column, DurableTables, ReconciledFile, SharedFileTables, TableSchema, TypedTable,
    WriterOwnedStore,
)
from bus_record_fixture import OWNER_LOOKUP
from cli_command import core
from conftest import ledger_one_failure_short, maintenance_held

NO_MAINTENANCE_HOLDER = NoMaintenanceHolder()


def reconcile(root: Path, holder: MaintenanceHolder = NO_MAINTENANCE_HOLDER,
              command: type[CliCommand] = ReconcileStoresCliCommand) -> subprocess.CompletedProcess:
    with holder.acting():
        return core("--root", str(root), *command.invocation())


def drop_one_index_each(root: Path) -> dict[str, str]:
    dropped = {}
    for path in ReconciledFile.existing_in(root):
        with closing(sqlite3.connect(path)) as db:
            index = db.execute("SELECT name FROM sqlite_master WHERE type='index'"
                               " AND sql IS NOT NULL ORDER BY name LIMIT 1").fetchone()
            if index:
                db.execute(f'DROP INDEX "{index[0]}"')
                db.commit()
                dropped[str(path.relative_to(root))] = index[0]
    assert dropped
    return dropped


def test_reconcile_names_every_store_it_carried_back_under_the_hold(private_comms):
    root = private_comms.root
    dropped = drop_one_index_each(root)
    with maintenance_held(root) as holder:
        completed = reconcile(root, holder)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert tuple(json.loads(completed.stdout)["rewritten"]) == tuple(sorted(dropped))
        for name, index in dropped.items():
            with closing(sqlite3.connect(root / name)) as db:
                assert db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (index,)).fetchone()
        assert json.loads(reconcile(root, holder).stdout)["rewritten"] == []


def test_a_schema_change_outside_the_hold_is_refused_naming_the_store(private_comms):
    root = private_comms.root
    dropped = drop_one_index_each(root)
    completed = reconcile(root)
    assert completed.returncode != 0
    assert "needs this root's maintenance hold" in completed.stdout + completed.stderr
    assert any(name in completed.stdout + completed.stderr for name in dropped)
    for name, index in dropped.items():
        with closing(sqlite3.connect(root / name)) as db:
            assert not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (index,)).fetchone()


def test_planned_reconcile_names_the_same_stores_and_writes_nothing(private_comms):
    """The installer asks this before its backup, under the hold that would let a write
    through: it must name what reconcile-stores rewrites and leave every byte alone."""
    root = private_comms.root
    dropped = drop_one_index_each(root)
    # A schema-named runtime projection is rebuilt by its runtime, never carried
    # or included in the installer's backup/reconcile set.
    projection = ActivityStore(root)
    with projection.writing() as db:
        db.execute('CREATE TABLE retired_derived_shape (unused INTEGER)')
    with maintenance_held(root) as holder:
        before = bus_files(LiveFilesRequest(root))
        completed = reconcile(root, holder, PlannedReconcileCliCommand)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert tuple(json.loads(completed.stdout)["rewritten"]) == tuple(sorted(dropped))
        assert bus_files(LiveFilesRequest(root)) == before
        completed = reconcile(root, holder)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert tuple(json.loads(completed.stdout)["rewritten"]) == tuple(sorted(dropped))
    with closing(sqlite3.connect(projection.path)) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='retired_derived_shape'").fetchone()


def test_read_back_under_the_hold_writes_nothing_on_a_drifted_bus(private_comms):
    """The installer reads the bus back before its carries, after the backup; a write the
    read made would be refused as a change no step names, so a correct install needs it to
    write nothing even where every store's schema differs."""
    root = private_comms.root
    drop_one_index_each(root)
    with maintenance_held(root) as holder:
        before = bus_files(LiveFilesRequest(root))
        completed = reconcile(root, holder, ReadBackCliCommand)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert bus_files(LiveFilesRequest(root)) == before


def test_a_settled_bus_reconciles_without_moving_committed_wal_frames(private_comms):
    """An owner that ends without the last close leaves its commits in the WAL. The practice
    copy is a SQLite backup holding none, so the live bus is settled before its snapshot;
    otherwise reconcile's own last close moves the frames into the file, and the carry
    refuses a change no step names."""
    root = private_comms.root
    # The child rewrites one row in place: frames in the WAL, the same rows in the store.
    framed = subprocess.run(
        [sys.executable, "-c",
         "import os, sqlite3, sys\n"
         "for path in sys.argv[1:]:\n"
         "    db = sqlite3.connect(path, isolation_level=None)\n"
         "    if db.execute('PRAGMA journal_mode').fetchone() != ('wal',):\n"
         "        continue\n"
         "    db.execute('PRAGMA wal_autocheckpoint=0')\n"
         "    tables = db.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")\n"
         "    for (table,) in tables.fetchall():\n"
         "        try:\n"
         "            if db.execute(f'UPDATE \"{table}\" SET rowid=rowid').rowcount:\n"
         "                print(path, flush=True)\n"
         "                os._exit(0)\n"
         "        except sqlite3.OperationalError:\n"
         "            pass\n"
         "os._exit(1)\n",
         *map(str, ReconciledFile.existing_in(root))],
        capture_output=True, text=True, check=True, timeout=60)
    database = Path(framed.stdout.strip())
    assert SQLiteFile.wal_path(database).stat().st_size
    with maintenance_held(root) as holder:
        SQLiteFile.settle(database)
        before = bus_files(LiveFilesRequest(root))
        completed = reconcile(root, holder)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert json.loads(completed.stdout)["rewritten"] == []
        assert bus_files(LiveFilesRequest(root)) == before


def test_private_carry_plans_and_restores_missing_native_tables(private_comms):
    root = private_comms.root
    database = CoordinationStore.path_in(root)
    with Coordination(database) as store:
        store.participants.register(OWNER_LOOKUP, committed=True)
        retained = store.participants.get(OWNER_LOOKUP)
        store.session._connection.execute(f"DROP TABLE {TriageNativeSource.declared_name}")

    with maintenance_held(root) as holder:
        before = bus_files(LiveFilesRequest(root))
        planned = reconcile(root, holder, PlannedReconcileCliCommand)
        assert planned.returncode == 0, planned.stdout + planned.stderr
        assert json.loads(planned.stdout)["rewritten"] == [database.name]
        assert bus_files(LiveFilesRequest(root)) == before
        carried = reconcile(root, holder)
        assert carried.returncode == 0, carried.stdout + carried.stderr
        assert json.loads(carried.stdout)["rewritten"] == [database.name]
        assert json.loads(reconcile(
            root, holder, PlannedReconcileCliCommand).stdout)["rewritten"] == []

    with Coordination(database) as store:
        assert store.participants.get(OWNER_LOOKUP) == retained
        PrivateCoordinatorSchema.require_installed(store.session._connection)


def test_public_reconcile_does_not_install_native_state(tmp_path):
    root = tmp_path / "public"
    Bus.install(Bus.path_in(root))
    database = CoordinationStore.path_in(root)
    with CoordinationSession(database):
        pass
    before = bus_files(LiveFilesRequest(root))
    planned = reconcile(root, command=PlannedReconcileCliCommand)
    assert planned.returncode == 0, planned.stdout + planned.stderr
    assert json.loads(planned.stdout)["rewritten"] == []
    carried = reconcile(root)
    assert carried.returncode == 0, carried.stdout + carried.stderr
    assert json.loads(carried.stdout)["rewritten"] == []
    assert bus_files(LiveFilesRequest(root)) == before
    with closing(sqlite3.connect(database)) as db:
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name=?", (NativeRuntimeInput.declared_name,)
        ).fetchone()


def test_planned_reconcile_refuses_a_writer_owned_store_its_declaration_cannot_open(tmp_path):
    """Toad's laptop toad.db, 10-24: a pre-STRICT file the declaration refuses fails the
    install's plan, naming the file, before any owner stops; the file itself is untouched."""
    class LaunchTables(SharedFileTables, DurableTables):
        """A view's own durable file, outside the bus."""

    @dataclass(frozen=True)
    class LaunchRow(LaunchTables, TypedTable, declared_name="launch_rows"):
        key: Annotated[str, Column(primary_key=True)]
        count: int

    stored = tmp_path / "state" / "launch.db"
    stored.parent.mkdir()

    class LaunchStore(WriterOwnedStore):
        @classmethod
        def path(cls) -> Path:
            return stored

        @classmethod
        def table_schema(cls) -> type[TableSchema]:
            return LaunchTables

    with closing(sqlite3.connect(stored)) as db:
        db.execute("CREATE TABLE launch_rows (key TEXT NOT NULL, count TEXT DEFAULT '0', "
                   "PRIMARY KEY (key))")
        db.execute("INSERT INTO launch_rows VALUES ('first', '1')")
        db.commit()
    before = stored.read_bytes()
    root = tmp_path / "bus"
    root.mkdir()
    try:
        with pytest.raises(RelationViolationError, match=str(stored)):
            PlannedReconcileCliCommand().answer(root)
        assert stored.read_bytes() == before
    finally:
        stored.unlink()
    assert PlannedReconcileCliCommand().answer(root).rewritten == ()


def test_the_goal_ledger_carries_a_family_that_gained_a_member(private_comms):
    """Install 13: the ledger kept a failure CHECK one member short, nothing carried it, and
    every opening refused. Its owner is a reconciled file, so the install carries it."""
    root = private_comms.root
    path = ledger_one_failure_short(root)
    with maintenance_held(root) as holder:
        completed = reconcile(root, holder)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert str(path.relative_to(root)) in json.loads(completed.stdout)["rewritten"]
    GoalAttemptStore.at(path.parent)
