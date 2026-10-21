"""Rename the durable goal decision table once; keep its rows and launch fences.

The old spelling exists only at this carry boundary. Core derives the new name
from GoalWorkDecision. Delete this step after every retained bus has run it.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

from agent_comms.errors import RelationViolationError
from agent_comms.goal_attempts import GoalAttemptStore, GoalWorkDecision
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import SQLiteFile
from agent_comms.typed_table import SqlValue


RETIRED_TABLE = "goal_human_decision"


class GoalWorkDecisions(Carry):
    """Carry the table after old owners stop, before the new runtime reads it."""

    @staticmethod
    def ledger_path(root: Path) -> Path:
        return GoalAttemptStore.path_in(root / GoalAttemptStore.root_directory)

    def backup_files(self) -> tuple[Path, ...]:
        root = self.root
        paths = tuple(path for path in SQLiteFile.files(self.ledger_path(root)) if path.exists())
        for path in paths:
            self.require_confined(root, path)
        return paths

    def carry(self) -> Carried:
        root, apply = self.root, self.apply
        path = self.ledger_path(root)
        self.require_confined(root, path)
        if not path.exists():
            return Carried(0)
        with closing(GoalAttemptStore.database(path)) as db:
            with SQLiteFile.write_transaction(db):
                retired_present = SqlValue.for_annotation(bool).read(db.execute(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)",
                    (RETIRED_TABLE,),
                ))[0]
                current_present = GoalWorkDecision.present(db)
                if retired_present and current_present:
                    raise RelationViolationError("Both goal decision tables exist; refuse to choose rows")
                if not retired_present:
                    if not current_present:
                        raise RelationViolationError("The goal ledger has no decision table")
                    return Carried(0)
                if apply:
                    db.execute(f'ALTER TABLE "{RETIRED_TABLE}" RENAME TO "{GoalWorkDecision.declared_name}"')
        rewritten = (str(path.relative_to(root)),) if apply else ()
        return Carried(1, rewritten=rewritten)
