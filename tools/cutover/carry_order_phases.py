"""Give saved folded orders their original phase before the inherited-field reader runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_comms.todos import FoldedProblemOrder, Todo
from agent_comms.typed_table import TypedRow

from .carry import Carried, TodoRows


@dataclass(frozen=True)
class FoldedPhase(TypedRow):
    id: str
    path: str


class OrderPhases(TodoRows):
    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        changed = 0
        parameters = (FoldedProblemOrder.declared_name,)
        with cls.todo_rows(root, apply=apply) as db:
            if db is None:
                return Carried(0)
            # Inner folds first: a parent then reads the child's converted phase,
            # not a stale decoded copy captured before the inner update.
            rows = FoldedPhase.read(
                db.execute(
                    f'SELECT t.id, n.path AS path FROM "{Todo.declared_name}" t,'
                    " json_tree(t.\"order\") n WHERE n.key='kind' AND n.value=?"
                    " ORDER BY length(n.path) DESC",
                    parameters,
                )
            )
            for row in rows:
                phase, original = row.path + ".serves", row.path + ".original.serves"
                changed += db.execute(
                    f'UPDATE "{Todo.declared_name}"'
                    ' SET "order"=json_set("order", ?, json_extract("order", ?))'
                    ' WHERE id=? AND json_type("order", ?) IS NULL'
                    ' AND json_type("order", ?) IS NOT NULL',
                    (phase, original, row.id, phase, original),
                ).rowcount
        return cls.carried(root, changed, {"folded_phases": changed}, apply=apply)


if __name__ == "__main__":
    OrderPhases.main()
