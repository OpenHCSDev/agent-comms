"""One-shot P391 conversion, with agents stopped and todos.sqlite3 backed up.

PYTHONPATH=src python tools/cutover/order_scope.py COPIED_OR_STOPPED_BUS_ROOT

No resident compatibility reader is installed. Delete this tool after Core runs
it at installation. Historical owner references are preserved, not reclassified
against today's source; fresh fix orders validate their class declarations.
"""
from __future__ import annotations

import argparse
import json
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.change_report import FileLine
from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec
from agent_comms.locked_store import SQLiteFile
from agent_comms.todos import CheckedProblemOrder, FixScope, FixShape, ProblemOrder, Todo, TodoStore, TriageScope
from agent_comms.typed_table import Column, JsonStorage, TextStorage, TypedRow

TRIAGE_PROBLEMS = frozenset({"P362", "P377", "P378", "P379"})


@dataclass(frozen=True)
class StoredOrder(TypedRow):
    """SQLite decodes one pre-scope declaration selected by its family's tag."""

    problem: str
    path: str
    owner: Annotated[FileLine, Column(storage=TextStorage)]

    def scope_json(self) -> str:
        scope = (TriageScope() if self.problem in TRIAGE_PROBLEMS
                 else FixScope(FixShape(parents=(self.owner,), members=())))
        return json.dumps(FieldCodec.encode(scope))


def convert(root: Path) -> int:
    path = TodoStore.path_in(root)
    kinds = tuple(member.declared_name for member in ProblemOrder.members_with(CheckedProblemOrder))
    placeholders = ",".join("?" for _ in kinds)
    table = Todo.declared_name
    changed = 0
    identity = Todo._column_list(("id",))
    with closing(CoordinationStore.writer(path)) as db, SQLiteFile.write_transaction(db):
        for declared in Todo._fields():
            if not issubclass(declared.storage, JsonStorage):
                continue
            column = Todo._column_list((declared.name,))
            orders = StoredOrder.read(db.execute(
                f'SELECT todo.{identity} AS problem, entry.path AS path, '
                f"json_extract(todo.{column}, entry.path || '.owner') AS owner "
                f'FROM "{table}" AS todo, json_tree(todo.{column}) AS entry '
                f"WHERE entry.key='kind' AND entry.value IN ({placeholders}) "
                f"AND json_type(todo.{column}, entry.path || '.scope') IS NULL", kinds))
            for order in orders:
                db.execute(
                    f'UPDATE "{table}" SET {column}=json_set({column}, ?, json(?)) WHERE {identity}=?',
                    (order.path + ".scope", order.scope_json(), order.problem))
                changed += 1
        # Decode every resulting canonical row before committing any conversion.
        for _ in Todo.stream(db):
            pass
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    arguments = parser.parse_args()
    print(f"Added explicit order scopes to {convert(arguments.root)} stored declarations")


if __name__ == "__main__":
    main()
