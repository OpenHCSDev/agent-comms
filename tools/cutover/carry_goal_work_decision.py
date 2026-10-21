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


RETIRED_TABLE = "goal_human_decision"


class GoalWorkDecisions(Carry):
    """Carry the table after old owners stop, before the new runtime reads it."""

    @staticmethod
    def ledger_path(root: Path) -> Path:
        return GoalAttemptStore.path_in(root / GoalAttemptStore.root_directory)

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        paths = tuple(path for path in SQLiteFile.files(cls.ledger_path(root)) if path.exists())
        for path in paths:
            cls.require_confined(root, path)
        return paths

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = cls.ledger_path(root)
        cls.require_confined(root, path)
        if not path.exists():
            return Carried(0)
        with closing(GoalAttemptStore.database(path)) as db:
            with SQLiteFile.write_transaction(db):
                names = {name for name, in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?)",
                    (RETIRED_TABLE, GoalWorkDecision.declared_name),
                )}
                if RETIRED_TABLE in names and GoalWorkDecision.declared_name in names:
                    raise RelationViolationError("Both goal decision tables exist; refuse to choose rows")
                if RETIRED_TABLE not in names:
                    if GoalWorkDecision.declared_name not in names:
                        raise RelationViolationError("The goal ledger has no decision table")
                    return Carried(0)
                if apply:
                    db.execute(f'ALTER TABLE "{RETIRED_TABLE}" RENAME TO "{GoalWorkDecision.declared_name}"')
        rewritten = (str(path.relative_to(root)),) if apply else ()
        return Carried(1, rewritten=rewritten)
