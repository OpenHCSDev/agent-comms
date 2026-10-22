"""The installer's reconcile, run the way the installer runs it: the target's CLI on a bus
whose stored schemas differ. Every file it rewrites is named, so the backup and the
rehearsal account for it like a carry's rewrite; nothing the CLI opens first reconciles
a store behind the verb's back."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import closing

from agent_comms.typed_table import ReconciledFile


def reconcile(root) -> tuple[str, ...]:
    completed = subprocess.run(
        [sys.executable, "-c", "from agent_comms.cli import main; main()", "--root", str(root),
         "reconcile-stores"], capture_output=True, text=True, timeout=120, check=True)
    return tuple(json.loads(completed.stdout)["rewritten"])


def test_reconcile_names_every_store_it_carried_back_onto_its_declaration(private_comms):
    root = private_comms.root
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
    assert reconcile(root) == tuple(sorted(dropped))
    for name, index in dropped.items():
        with closing(sqlite3.connect(root / name)) as db:
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (index,)).fetchone()
    assert reconcile(root) == ()
