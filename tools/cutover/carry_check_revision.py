"""Open orders' check lines stop choosing their revision (P166).

Core now runs an order's check at each commit it reads, in a detached checkout of
that commit, and refuses a new check line that checks out a revision. An open
order written before that kept a leading ``git fetch ... && git checkout ... &&``;
its check drops those leading commands. No other field of a saved order changes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

from . import carry
from .carry import Carried

from agent_comms.goals import OrderCheck
from agent_comms.todos import Todo

LEADING: frozenset[str] = frozenset({"fetch", *OrderCheck.REVISION_COMMANDS})


def revision_free(command: str) -> str:
    """``command`` without its leading git fetch and checkout commands."""
    rest = command
    while True:
        leading, separator, after = rest.partition("&&")
        words = OrderCheck.simple_commands(leading)
        if not separator or any(OrderCheck.git_command(each) not in LEADING for each in words):
            return rest
        rest = after.strip()


class CheckRevisions(carry.TodoRows):
    """Each open order's check line drops the checkout that chose its revision."""


    @classmethod
    def carry_rows(cls, root: Path, db: sqlite3.Connection, *, apply: bool) -> Carried:
        changed = 0
        for todo in cast("list[Todo]", Todo.select(db)):
            check = todo.order.check
            command = revision_free(check.command)
            if not todo.pending or command == check.command:
                continue
            changed += 1
            order = replace(todo.order, check=replace(check, command=command))
            Todo.update(db, where="{id} = ?", parameters=(todo.id,), order=order)
        return cls.carried(root, changed, {"open": changed}, apply=apply)


if __name__ == "__main__":
    CheckRevisions.main()
