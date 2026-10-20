"""Remove retired first-call state from current, folded and historical orders (P325).

The deadline and transcript own first-call evidence. This conversion removes only
its obsolete stored copy before the strict production order decoder runs.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import get_args, get_origin, get_type_hints

from agent_comms.todos import ProblemOrder, Todo
from agent_comms.typed_table import TypedRow

from . import carry


@dataclass(frozen=True)
class SavedKey(TypedRow):
    """One obsolete JSON key's row and path, not its retired value type."""

    id: str
    path: str


class FirstCalls(carry.TodoRows):
    """Strip the copy throughout both declared locations of saved orders."""

    @classmethod
    def converted(cls, db: sqlite3.Connection) -> Mapping[str, int]:
        changed = 0
        hints = get_type_hints(Todo)
        for field in Todo._fields():
            declared = hints[field.name]
            if not (
                declared is ProblemOrder
                or (
                    get_origin(declared) is tuple
                    and issubclass(get_args(declared)[0], ProblemOrder)
                )
            ):
                continue
            column = field.wire_name
            saved = SavedKey.read(
                db.execute(
                    f'SELECT t.id, n.fullkey AS path FROM "{Todo.declared_name}" t,'
                    f' json_tree(t."{column}") n WHERE n.key=?'
                    " ORDER BY length(n.fullkey) DESC",
                    ("first_call",),
                )
            )
            for row in saved:
                db.execute(
                    f'UPDATE "{Todo.declared_name}"'
                    f' SET "{column}"=json_remove("{column}", ?) WHERE id=?',
                    (row.path, row.id),
                )
            changed += len(saved)
        return {"first_call_keys": changed}


if __name__ == "__main__":
    FirstCalls.main()
