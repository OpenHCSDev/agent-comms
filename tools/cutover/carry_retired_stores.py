"""Retire superseded stores after their writers and consumers have been removed."""

from pathlib import Path

from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import PatternFile, SQLiteFile
from agent_comms.typed_table import HeldBusFile


class RetiredSQLiteStore(SQLiteFile, PatternFile):
    """Declared SQLite snapshot behavior remains available throughout rollback."""


class RetiredTodoAllocationSnapshot(RetiredSQLiteStore):
    filename = "todos.sqlite3.before-allocation-{created}"


class RetiredTranscriptReplyIndex(RetiredSQLiteStore):
    filename = "transcript_reply_index.v4.sqlite3"


class RetiredWakeCandidateIndex(RetiredSQLiteStore):
    filename = "wake_candidate{revision}.sqlite3"


class RetiredBusPageIndex(RetiredSQLiteStore):
    filename = "bus_page_index{revision}.sqlite3"


class RetiredBusRouteCounts(RetiredSQLiteStore):
    filename = "bus_route_counts{revision}.sqlite3"


class RetiredBusCheckpoint(RetiredSQLiteStore):
    filename = "private_bus_checkpoint.sqlite3"


class RetiredWrappedOwnerReleases(RetiredSQLiteStore):
    root_directory = "owner_release_receipts.json"
    filename = "owner_releases.sqlite3"


class RetiredStores(Carry):
    opened_files = (RetiredSQLiteStore,)

    def carry(self, *, apply: bool) -> Carried:
        owners = {path: owner for path, owner in SQLiteFile.existing_in(self.root).items()
                  if issubclass(owner, RetiredSQLiteStore)}
        rewritten: list[Path] = []
        for path, owner in owners.items():
            self.require_confined(self.root, path)
            if apply:
                HeldBusFile(owner, path).require()
                for member in owner.files(path):
                    if member.exists():
                        member.unlink()
                        rewritten.append(member.relative_to(self.root))
        return Carried(len(owners), rewritten=tuple(rewritten))
