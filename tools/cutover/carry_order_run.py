"""Saved problem orders gain the closing run they declared (P511).

Orders written before P511 declared no run. Each becomes a mechanical run,
or an application run when its body carries a ``Works when:`` line. No other
field of a saved order is changed. Production readers accept only the new
declared schema.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from contextlib import closing
from pathlib import Path

from cutover.carry import Carried, Carry

from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec
from agent_comms.todos import ApplicationOrderRun, MechanicalOrderRun, OrderRun, Todo, TodoStore

DECLARED = re.compile(rf"(?m)^\s*{re.escape(OrderRun.label)}\s*(.+?)\s*$")


def declared_run(body: str) -> OrderRun:
    found = DECLARED.search(body)
    return MechanicalOrderRun() if found is None else ApplicationOrderRun(found.group(1))


def carry(root: Path, *, apply: bool) -> Counter[str]:
    path = TodoStore.path_in(root)
    counts: Counter[str] = Counter()
    if not path.exists():
        return counts
    with closing(CoordinationStore.writer(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            table = Todo.declared_name
            rows = db.execute(
                f'SELECT "id", "text", "order" FROM "{table}" '
                """WHERE json_type("order", '$.run') IS NULL"""
            ).fetchall()
            for todo_id, body, saved in rows:
                order = json.loads(saved)
                run = declared_run(body)
                counts[run.declared_name] += 1
                db.execute(
                    f'UPDATE "{table}" SET "order" = ? WHERE "id" = ?',
                    (json.dumps({**order, "run": FieldCodec.encode(run)}), todo_id),
                )
            for _row in Todo.stream(db):
                pass
            if apply:
                db.commit()
            else:
                db.rollback()
            return counts
        except BaseException:
            db.rollback()
            raise


class OrderRuns(Carry):
    """Saved problem orders gain their declared closing run; no other stored field changes."""

    after = "carry_todo_voids"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        counts = carry(root, apply=apply)
        changed = sum(counts.values())
        rewritten = tuple(path.name for path in root.glob(f"{TodoStore.path_in(root).name}*"))
        return Carried(changed, dict(counts), rewritten if changed and apply else ())


if __name__ == "__main__":
    OrderRuns.main()
