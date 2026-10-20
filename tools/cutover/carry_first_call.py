"""Remove retired first-call state from current, folded and historical orders (P325).

The deadline and transcript own first-call evidence. This conversion removes only
its obsolete stored copy before the strict production order decoder runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_comms.field_codec import boundary_decoder
from agent_comms.todos import Todo
from agent_comms.typed_table import TypedRow

from . import carry
from .carry import Carried


@dataclass(frozen=True)
class SavedKey(TypedRow):
    """One obsolete JSON key's row and path, not its retired value type."""

    id: str
    path: str


class FirstCalls(carry.TodoRows):
    """Strip the copy throughout both declared locations of saved orders."""

    @classmethod
    @boundary_decoder
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        changed = 0
        with cls.todo_rows(root, apply=apply) as db:
            if db is not None:
                for field in Todo._fields_named(("order", "prior_orders")):
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
        return cls.carried(root, changed, {"first_call_keys": changed}, apply=apply)


if __name__ == "__main__":
    FirstCalls.main()
