"""Install declaration-owned goal-history indexes once; never rewrite journal rows.

Default is read-only planning. Stop old writers before --apply and install the
paired runtime afterward: old strict writers do not accept additional indexes.
This tool refuses missing/changed tables, foreign objects and changed indexes.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import stat

from agent_comms.coordination_database import CoordinationStore
from agent_comms.goal_history import GoalHistoryEntry, GoalHistoryError, GoalHistoryStore
from agent_comms.registry_store import RegistryStore
from agent_comms.typed_table import SQLiteSchemaObject


def install_indexes(registry_path: Path, *, apply: bool = False) -> dict:
    path = registry_path.parent / "goal_history.sqlite3"
    with RegistryStore(registry_path).locked():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise GoalHistoryError("Goal history must be an existing private regular file.")
        connection = (CoordinationStore.writer(path, isolation_level=None) if apply
                      else CoordinationStore.read_snapshot(path))
        with closing(connection):
            if apply:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=EXTRA")
                connection.execute("BEGIN IMMEDIATE")
            actual = SQLiteSchemaObject.read(connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%'"
            ))
            missing = GoalHistoryEntry.missing_indexes(actual)
            if apply:
                for statement in missing.values():
                    connection.execute(statement)
                connection.commit()
        if apply:
            GoalHistoryStore._sync_file(path)
            if os.name == "posix":
                GoalHistoryStore._sync_directory(path.parent)
        return {"apply": apply, "indexes": tuple(missing), "journal_rows_changed": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    print(json.dumps(install_indexes(arguments.registry, apply=arguments.apply)))


if __name__ == "__main__":
    main()
