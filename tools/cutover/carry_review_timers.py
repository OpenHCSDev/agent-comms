"""Todo rows trade the order deadline for the commander's review timer, once.

The deadline column becomes ``review_at`` with the same instant: the first review is when the
order used to be due. The clock strikes are deleted from Core, so a recorded stall, missed
deadline or missed first call becomes the plain strike it was recorded as, with its reason,
time, actor and evidence intact; a void of one still names the same strike. Core's watcher is
named for what it does now, ``core_watcher``. Delete once every bus has run it.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.installer.carry import Carried, Carry
from agent_comms.thread_identity import CoreWatcherIncarnation
from agent_comms.todos import StrikeTodo, Todo, TodoStore

# The strike kinds Core no longer declares; only their stored names remain.
CLOCK_STRIKES = frozenset({"stall_strike", "missed_deadline_strike", "missed_first_call_strike"})
RETIRED_WATCHER = "stall_watcher"


def carried(value: object) -> object:
    """``value`` with every clock strike made a plain strike and the watcher renamed."""
    if isinstance(value, list):
        return [carried(item) for item in value]
    if not isinstance(value, dict):
        return value
    entries = {key: carried(item) for key, item in value.items()}
    if entries.get("kind") in CLOCK_STRIKES:
        entries.pop("order_id", None)
        entries["kind"] = StrikeTodo.declared_name
    watcher = CoreWatcherIncarnation.watcher()
    # A thread incarnation is stored whole as name and created_at, and flattened into an
    # assignment as parent and parent_created.
    for name, birth in (("name", "created_at"), ("parent", "parent_created")):
        if entries.get(name) == RETIRED_WATCHER and entries.get(birth) == watcher.WIRE_BIRTH:
            entries[name] = watcher.name
    return entries


def converted(columns: list[str], row: tuple) -> tuple[object, ...]:
    """The stored row in the declared column order, each value carried in its wire form."""
    stored = dict(zip(columns, row))
    stored["review_at"] = stored.pop("deadline")
    return tuple(
        None if stored[item.wire_name] is None
        else item.storage.to_sql(carried(item.storage.from_sql(stored[item.wire_name])))
        for item in Todo._fields())


def carry(root: Path, *, apply: bool) -> int:
    path = TodoStore.path_in(root)
    if not path.exists():
        return 0
    with closing(CoordinationStore.writer(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            cursor = db.execute(f'SELECT * FROM "{Todo.declared_name}"')
            columns = [column[0] for column in cursor.description]
            if "deadline" not in columns:
                db.rollback()
                return 0
            records = [converted(columns, row) for row in cursor.fetchall()]
            Todo.drop(db)
            Todo.create(db)
            names = tuple(item.name for item in Todo._fields())
            db.executemany(
                f'INSERT INTO "{Todo.declared_name}" ({Todo._column_list(names)}) '
                f'VALUES ({", ".join("?" for _ in names)})', records)
            if (decoded := sum(1 for _row in Todo.stream(db))) != len(records):
                raise ValueError(f"{len(records)} todo rows carried, {decoded} decoded")
            if apply:
                db.commit()
            else:
                db.rollback()
            return len(records)
        except BaseException:
            db.rollback()
            raise


class ReviewTimers(Carry):
    """Todo rows keep their instant as the review timer; clock strikes become plain strikes."""

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return tuple(root.glob(f"{TodoStore.path_in(root).name}*"))

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        changed = carry(root, apply=apply)
        rewritten = tuple(path.name for path in root.glob(f"{TodoStore.path_in(root).name}*"))
        return Carried(changed, {}, rewritten if changed and apply else ())
