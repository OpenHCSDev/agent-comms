"""The installer's reconcile, run the way the installer runs it: the target's CLI under the
root's maintenance hold, on a bus whose stored schemas differ. Every file it rewrites is
named, so the backup and the rehearsal account for it like a carry's rewrite; nothing the
CLI opens first reconciles a store behind the verb's back. Without the hold, a schema that
differs is refused, naming the store, and the file stays as it was."""
from __future__ import annotations

import hashlib
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
    ReconcileStoresCliCommand,
)
from agent_comms.maintenance_barrier import HOLDER_NONCE
from agent_comms.typed_table import ReconciledFile
from conftest import maintenance_held


def reconcile(root: Path, holder: str = "",
              command: type[CliCommand] = ReconcileStoresCliCommand) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent_comms.cli", "--root", str(root),
         *command.invocation()], capture_output=True, text=True, timeout=120,
        env={**os.environ, HOLDER_NONCE: holder})


def digests(root: Path) -> dict[Path, str]:
    return {path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


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
    with maintenance_held(root) as nonce:
        before = digests(root)
        completed = reconcile(root, nonce, PlannedReconcileCliCommand)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert tuple(json.loads(completed.stdout)["rewritten"]) == tuple(sorted(dropped))
        assert digests(root) == before
