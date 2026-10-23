"""The installer's reconcile, run the way the installer runs it: the target's CLI under the
root's maintenance hold, on a bus whose stored schemas differ. Every file it rewrites is
named, so the backup and the rehearsal account for it like a carry's rewrite; nothing the
CLI opens first reconciles a store behind the verb's back. Without the hold, a schema that
differs is refused, naming the store, and the file stays as it was."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from agent_comms.cli_commands import (
    CliCommand,
    PlannedReconcileCliCommand,
    ReadBackCliCommand,
    ReconcileStoresCliCommand,
)
from agent_comms.activity import ActivityStore
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordinator import Coordination
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.private_runtime_schema import PrivateCoordinatorSchema
from agent_comms.triage_native_sources import TriageNativeSources
from agent_comms.bus import Bus
from agent_comms.maintenance_barrier import HOLDER_NONCE
from agent_comms.installer.backup import bus_files
from agent_comms.typed_table import ProjectionProducer, ReconciledFile
from conftest import maintenance_held


def reconcile(root: Path, holder: str = "",
              command: type[CliCommand] = ReconcileStoresCliCommand) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent_comms.cli", "--root", str(root),
         *command.invocation()], capture_output=True, text=True, timeout=120,
        env={**os.environ, HOLDER_NONCE: holder})


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
    with maintenance_held(root) as nonce:
        completed = reconcile(root, nonce)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert tuple(json.loads(completed.stdout)["rewritten"]) == tuple(sorted(dropped))
        for name, index in dropped.items():
            with closing(sqlite3.connect(root / name)) as db:
                assert db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (index,)).fetchone()
        assert json.loads(reconcile(root, nonce).stdout)["rewritten"] == []


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
    projection = ActivityStore(ActivityStore.path_in(root))
    with projection.writing() as db:
        db.execute('CREATE TABLE retired_derived_shape (unused INTEGER)')
    with maintenance_held(root) as nonce:
        before = bus_files(root)
        completed = reconcile(root, nonce, PlannedReconcileCliCommand)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert tuple(json.loads(completed.stdout)["rewritten"]) == tuple(sorted(dropped))
        assert bus_files(root) == before
        completed = reconcile(root, nonce)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert tuple(json.loads(completed.stdout)["rewritten"]) == tuple(sorted(dropped))
    with closing(sqlite3.connect(projection.path)) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='retired_derived_shape'").fetchone()


def test_planned_reconcile_names_the_bus_whose_build_stamp_reconcile_rewrites(private_comms):
    """Install 23: a new build with unchanged producer source verifies by digest, but the
    reconcile restamps the build; the plan must name that write, or the rehearsal never
    copies the bus and the live carry rewrites a file the rehearsal did not."""
    root = private_comms.root
    bus = Bus.path_in(root)
    with closing(sqlite3.connect(bus)) as db:
        ProjectionProducer.update(db, where="1=1", parameters=(), commit="0" * 40)
        db.commit()
    rewritten = [str(bus.relative_to(root))]
    with maintenance_held(root) as nonce:
        before = bus_files(root)
        planned = reconcile(root, nonce, PlannedReconcileCliCommand)
        assert planned.returncode == 0, planned.stdout + planned.stderr
        assert json.loads(planned.stdout)["rewritten"] == rewritten
        assert bus_files(root) == before
        assert json.loads(reconcile(root, nonce).stdout)["rewritten"] == rewritten
        assert json.loads(reconcile(root, nonce, PlannedReconcileCliCommand).stdout)["rewritten"] == []


def test_read_back_under_the_hold_writes_nothing_on_a_drifted_bus(private_comms):
    """The installer reads the bus back before its carries, after the backup; a write the
    read made would be refused as a change no step names, so a correct install needs it to
    write nothing even where every store's schema differs."""
    root = private_comms.root
    drop_one_index_each(root)
    with maintenance_held(root) as nonce:
        before = bus_files(root)
        completed = reconcile(root, nonce, ReadBackCliCommand)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert bus_files(root) == before


def test_private_carry_plans_and_restores_missing_native_tables(private_comms):
    root = private_comms.root
    database = CoordinationStore.path_in(root)
    with Coordination(database) as store:
        store.participants.register("retained", committed=True)
        retained = store.participants.get("retained")
        store.session._connection.execute(f"DROP TABLE {TriageNativeSources.declared_name}")

    with maintenance_held(root) as nonce:
        before = bus_files(root)
        planned = reconcile(root, nonce, PlannedReconcileCliCommand)
        assert planned.returncode == 0, planned.stdout + planned.stderr
        assert json.loads(planned.stdout)["rewritten"] == [database.name]
        assert bus_files(root) == before
        carried = reconcile(root, nonce)
        assert carried.returncode == 0, carried.stdout + carried.stderr
        assert json.loads(carried.stdout)["rewritten"] == [database.name]
        assert json.loads(reconcile(root, nonce, PlannedReconcileCliCommand).stdout)["rewritten"] == []

    with Coordination(database) as store:
        assert store.participants.get("retained") == retained
        PrivateCoordinatorSchema.require_installed(store.session._connection)


def test_public_reconcile_does_not_install_native_state(tmp_path):
    root = tmp_path / "public"
    Bus.install(Bus.path_in(root))
    database = CoordinationStore.path_in(root)
    with CoordinationSession(database):
        pass
    before = bus_files(root)
    planned = reconcile(root, command=PlannedReconcileCliCommand)
    assert planned.returncode == 0, planned.stdout + planned.stderr
    assert json.loads(planned.stdout)["rewritten"] == []
    carried = reconcile(root)
    assert carried.returncode == 0, carried.stdout + carried.stderr
    assert json.loads(carried.stdout)["rewritten"] == []
    assert bus_files(root) == before
    with closing(sqlite3.connect(database)) as db:
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name=?", (NativeRuntimeInput.declared_name,)
        ).fetchone()
