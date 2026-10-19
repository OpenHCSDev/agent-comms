"""Problems close at delivery; no saved order waits on an install (P168).

An order now closes when it is merged and checked; installing main is the install
schedule's work. A saved order in the removed ``installed`` state drops its works and
artifact evidence and reads as delivered. A problem still open on a delivered order
closes, as its delivery closes it now. Production readers accept only the new schema.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from cutover import carry
from cutover.carry import Carried

from agent_comms.field_codec import FieldCodec
from agent_comms.todos import DoneTodoState, ProblemOrder, Todo, TodoState
from agent_comms.typed_table import TypedRow


@dataclass(frozen=True)
class SavedOrder(TypedRow):
    """A todo's order and revision as stored, read before the order can decode."""

    id: str
    revision: int
    order: str


class DeliveredOrders(carry.TodoRows):
    """Installed orders read as delivered, and open delivered problems close."""

    after = "carry_problem_goal_prose"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        counts: Counter[str] = Counter()
        with cls.todo_rows(root, apply=apply) as db:
            if db is not None:
                installed = cast("list[SavedOrder]", Todo.select(
                    db, where="""json_extract("{order}", '$.kind') = 'installed'""",
                    row_type=SavedOrder))
                for row in installed:
                    saved = json.loads(row.order)
                    order = {key: value for key, value in saved.items()
                             if key not in ("works", "artifact")}
                    Todo.update(db, where="{id} = ?", parameters=(row.id,),
                                order=FieldCodec.decode(ProblemOrder, {**order, "kind": "delivered"}))
                    counts["installed"] += 1
                states = TodoState.open_names()
                open_delivered = cast("list[SavedOrder]", Todo.select(
                    db, where=f"""json_extract("{{order}}", '$.kind') = 'delivered'
                    AND "{{state}}" IN ({','.join('?' for _ in states)})""",
                    parameters=states, row_type=SavedOrder))
                for row in open_delivered:
                    Todo.update(db, where="{id} = ?", parameters=(row.id,), state=DoneTodoState,
                                assignment=None, revision=row.revision + 1)
                    counts["closed"] += 1
        return cls.carried(root, sum(counts.values()), dict(counts), apply=apply)


if __name__ == "__main__":
    DeliveredOrders.main()
