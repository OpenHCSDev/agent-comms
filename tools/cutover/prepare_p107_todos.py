"""Prepare the current Todo schema in a new file; never install or publish it.

Use only in a newly recorded cutover phase. Preserve the source and candidate
for the existing stopped-owner publisher's review. Delete after accepted carry.
This is not authority to rerun any historical P107 operation.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import fields
import json
import os
from pathlib import Path
import sqlite3

from agent_comms.coordination_database import CoordinationStore
from agent_comms.todos import Todo


def prepare(source: Path, candidate: Path) -> dict:
    added = {"deadline", "strikes"}
    declared = Todo._fields()
    previous = tuple(item for item in declared if item.name not in added)
    defaults = {item.name: item.default_factory() if callable(item.default_factory) else item.default
                for item in fields(Todo) if item.name in added}
    table = Todo.declared_name
    count = 0
    with CoordinationStore.reading(source) as original:
        cursor = original.execute(f'SELECT * FROM "{table}"')
        names = tuple(column[0] for column in cursor.description)
        if names != tuple(item.name for item in previous):
            raise ValueError("Source is not the exact pre-P107 Todo layout")
        os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        with closing(sqlite3.connect(candidate)) as target, target:
            Todo.create(target)
            columns = (*names, *(item.name for item in declared if item.name in added))
            suffix = tuple(item.encode(defaults[item.name]) for item in declared if item.name in added)
            statement = Todo.insertion_statement(columns)
            for row in cursor:
                for item, value in zip(previous, row, strict=True):
                    item.decode(value)
                target.execute(statement, (*row, *suffix))
                count += 1
            if len(Todo.select(target)) != count:
                raise ValueError("Candidate Todo count changed")
        cursor.close()
    return {"source": str(source), "candidate": str(candidate), "rows": count,
            "installed": False, "requires_stopped_owner_review": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.candidate), indent=2))


if __name__ == "__main__":
    main()
