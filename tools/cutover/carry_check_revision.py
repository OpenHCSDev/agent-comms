"""Open orders' check lines stop choosing their revision (P166).

Core now runs an order's check at each commit it reads, in a detached checkout of
that commit, and refuses a new check line that checks out a revision. An open
order written before that kept a leading ``git fetch ... && git checkout ... &&``;
its check drops those leading commands. No other field of a saved order changes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import cast

from cutover import carry
from cutover.carry import Carried

from agent_comms.goals import OrderCheck
from agent_comms.todos import Todo

LEADING: frozenset[str] = frozenset({"fetch", *OrderCheck.REVISION_COMMANDS})


def revision_free(command: str) -> str:
    """``command`` without its leading git fetch and checkout commands."""
    rest = command
    while (head := rest.partition("&&"))[1] and all(
        OrderCheck.git_command(words) in LEADING for words in OrderCheck.simple_commands(head[0])
    ):
        rest = head[2].strip()
    return rest


class CheckRevisions(carry.TodoRows):
    """Each open order's check line drops the checkout that chose its revision."""

    after = "carry_problem_goal_prose"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        counts: Counter[str] = Counter()
        with cls.todo_rows(root, apply=apply) as db:
            if db is not None:
                for todo in cast("list[Todo]", Todo.select(db)):
                    check = todo.order.check
                    if not todo.pending or (command := revision_free(check.command)) == check.command:
                        continue
                    counts["open"] += 1
                    order = replace(todo.order, check=replace(check, command=command))
                    Todo.update(db, where="{id} = ?", parameters=(todo.id,), order=order)
        return cls.carried(root, sum(counts.values()), dict(counts), apply=apply)


if __name__ == "__main__":
    CheckRevisions.main()
