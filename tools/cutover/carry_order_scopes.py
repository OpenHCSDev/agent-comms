"""Carry order scopes through agent-comms install, after old owners stop.

Delete this step once every bus has run it. Historical owner references are
preserved, not reclassified against today's source; fresh fix orders validate
their class declarations. No resident compatibility reader is installed.
"""
from __future__ import annotations

import json
from contextlib import closing, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.change_report import FileLine
from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.carry import Carried, Carry
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


class OrderScopes(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        paths = tuple(path for path in SQLiteFile.files(TodoStore.path_in(root)) if path.exists())
        for path in paths:
            cls.require_confined(root, path)
        return paths

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = TodoStore.path_in(root)
        cls.require_confined(root, path)
        if not path.exists():
            return Carried(0)
        kinds = tuple(member.declared_name for member in ProblemOrder.members_with(CheckedProblemOrder))
        placeholders = ",".join("?" for _ in kinds)
        table = Todo.declared_name
        changed = 0
        identity = Todo._column_list(("id",))
        connection = CoordinationStore.writer(path) if apply else CoordinationStore.read_snapshot(path)
        with closing(connection) as db:
            with SQLiteFile.write_transaction(db) if apply else nullcontext(db):
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
                        if apply:
                            db.execute(
                                f'UPDATE "{table}" SET {column}=json_set({column}, ?, json(?)) WHERE {identity}=?',
                                (order.path + ".scope", order.scope_json(), order.problem))
                        changed += 1
                if apply:
                    # Decode every resulting canonical row before committing any conversion.
                    for _ in Todo.stream(db):
                        pass
        rewritten = tuple(str(file.relative_to(root)) for file in SQLiteFile.files(path)) if apply and changed else ()
        return Carried(changed, rewritten=rewritten)
