"""Drop the connectivity table the coordinator no longer declares.

Schema installation creates declared tables and never drops undeclared ones, so
every coordination database written before the facet was deleted still holds the
table and its four triggers. Dropping the table drops its triggers and index.

Delete after every bus has run this step.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import SQLiteFile
from agent_comms.store_files import _store_lock

TABLE = "connectivity"


class DropConnectivity(Carry):
    @property
    def path(self) -> Path:
        return self.root / CoordinationStore.filename

    def backup_files(self) -> tuple[Path, ...]:
        self.require_confined(self.root, self.path)
        return (self.path,) if self.path.exists() else ()

    def carry(self, *, apply: bool) -> Carried:
        if not self.path.exists():
            return Carried(0)
        with _store_lock(self.path), closing(
            CoordinationStore.writer(self.path, isolation_level=None)
        ) as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
            ).fetchone():
                return Carried(0)
            (rows,) = db.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()
            if not apply:
                return Carried(1, {TABLE: rows})
            CoordinationStore.schema_change(self.path).require()
            with SQLiteFile.write_transaction(db):
                db.execute(f'DROP TABLE "{TABLE}"')
            return Carried(1, {TABLE: rows}, (CoordinationStore.filename,))
