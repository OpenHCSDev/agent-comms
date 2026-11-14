"""Put the coordinator in WAL mode.

The coordinator ran a rollback journal with synchronous=FULL: every commit synced the journal,
the file and the directory (the journal's deletion). A #comms ping makes about forty coordinator
commits; across one (BR5) the worker spent 126 of its 150 ms of syncs on this file, 203 syncs.
In WAL a commit is one sync of the log.

The rollback journal was chosen so fresh openers would not race over switching the mode after
the schema. WAL persists in the file header: this carry sets it once with every owner stopped,
and a new coordinator gets it on its private stage before publication, so no opener switches it.
"""

from contextlib import closing
from typing import ClassVar

from agent_comms.coordination.database import CoordinationStore
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.storage.locked_store import RootFile
from agent_comms.storage.typed_table import SQLiteJournalMode


class CoordinatorWal(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (CoordinationStore,)

    def carry(self, *, apply: bool) -> Carried:
        rollback = []
        for path in self.input_files():
            with closing(CoordinationStore.read_snapshot(path)) as db:
                if SQLiteJournalMode.read(db.execute("PRAGMA journal_mode")) != [
                    SQLiteJournalMode("wal")
                ]:
                    rollback.append(path)
        if apply:
            for path in rollback:
                CoordinationStore.write_ahead(path)
        return Carried(
            len(rollback),
            detail={"stores": len(rollback)},
            rewritten=tuple(path.relative_to(self.root) for path in rollback) if apply else (),
        )
