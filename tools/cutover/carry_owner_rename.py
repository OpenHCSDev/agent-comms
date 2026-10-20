"""Allow an owner's name to change without revoking its active attempts."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_tables.participants import OwnerGenerations

from agent_comms.installer.carry import Carried, Carry


class OwnerRename(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return (root / "coordination.sqlite3",)

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = root / "coordination.sqlite3"
        if not path.exists():
            return Carried(0)
        # The upgrade owns the wire lock and has stopped writers. Change only
        # the declared guard, leaving every attempt, generation and input intact.
        with closing(sqlite3.connect(path, timeout=0)) as db, db:
            if apply:
                db.execute("BEGIN EXCLUSIVE")
            name = "owner_generation_monotonic"
            rows = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
            ).fetchall()
            if len(rows) != 1:
                raise ValueError("Owner rename carry requires the original generation guard")
            ((original,),) = rows
            original = original.strip()
            declared = OwnerGenerations.schema_objects()[name].strip()
            if original == declared:
                return Carried(0)
            # Exact pre-P800 guard, not permission to replace arbitrary SQL.
            if hashlib.sha256((original + "\n").encode()).digest() != bytes.fromhex(
                "0db20f52302bc4a4a99c40d1a43d38b27cd75f384d73f680049d4cf0c67f86b3"
            ):
                raise ValueError("Owner rename carry refuses a changed generation guard")
            if apply:
                db.execute('DROP TRIGGER "owner_generation_monotonic"')
                db.execute(declared)
        return Carried(1, rewritten=("coordination.sqlite3",))
