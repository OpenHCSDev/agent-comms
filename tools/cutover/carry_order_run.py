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
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from cutover import carry
from cutover.carry import Carried

from agent_comms.field_codec import FieldCodec
from agent_comms.todos import ApplicationOrderRun, MechanicalOrderRun, OrderRun, ProblemOrder, Todo
from agent_comms.typed_table import TypedRow

DECLARED = re.compile(rf"(?m)^\s*{re.escape(OrderRun.label)}\s*(.+?)\s*$")


@dataclass(frozen=True)
class SavedOrder(TypedRow):
    """A todo's body and its order as stored, read before the order can decode."""

    id: str
    text: str
    order: str


def declared_run(body: str) -> OrderRun:
    found = DECLARED.search(body)
    return MechanicalOrderRun() if found is None else ApplicationOrderRun(found.group(1))


class OrderRuns(carry.TodoRows):
    """Saved problem orders gain their declared closing run; no other stored field changes."""

    after = "carry_todo_corrections"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        counts: Counter[str] = Counter()
        with cls.todo_rows(root, apply=apply) as db:
            if db is not None:
                lacking = """json_type("{order}", '$.run') IS NULL"""
                saved = cast(
                    "list[SavedOrder]", Todo.select(db, where=lacking, row_type=SavedOrder)
                )
                for row in saved:
                    run = declared_run(row.text)
                    counts[run.declared_name] += 1
                    order = {**json.loads(row.order), "run": FieldCodec.encode(run)}
                    Todo.update(
                        db,
                        where="{id} = ?",
                        parameters=(row.id,),
                        order=FieldCodec.decode(ProblemOrder, order),
                    )
        return cls.carried(root, sum(counts.values()), dict(counts), apply=apply)


if __name__ == "__main__":
    OrderRuns.main()
