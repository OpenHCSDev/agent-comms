"""Todo rows trade the order deadline for the commander's review timer, once.

The deadline column becomes ``review_at`` with the same instant: the first review is when the
order used to be due. The clock strikes are deleted from Core, so a recorded stall, missed
deadline or missed first call becomes the plain strike it was recorded as, with its reason,
time, actor and evidence intact; a void of one still names the same strike. Core's watcher
decodes under the name its class declares, so its old spelling is rewritten by the same pass.
Delete once every bus has run it.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, fields, replace
from functools import singledispatch
from pathlib import Path
from types import NoneType

from agent_comms.coordination_database import CoordinationStore
from agent_comms.installer.carry import Carried, Carry
from agent_comms.todos import Assignment, StrikeTodo, Todo, TodoStore


# These old declarations exist only at this one-time format boundary: the strike kinds Core
# no longer declares, so their stored rows decode before they are carried.
@dataclass(frozen=True, kw_only=True)
class ClockStrike(StrikeTodo, declared_name=None):
    """A strike Core used to record from a clock; it is carried as the plain strike it was."""


@dataclass(frozen=True, kw_only=True)
class StallStrike(ClockStrike):
    pass


@dataclass(frozen=True, kw_only=True)
class MissedDeadlineStrike(ClockStrike):
    order_id: str


@dataclass(frozen=True, kw_only=True)
class MissedFirstCallStrike(ClockStrike):
    order_id: str


@singledispatch
def assigned(assignment: Assignment) -> Assignment:
    """An assignment flattens its parent to a name; the parent's decoded identity names it."""
    return replace(assignment, parent=assignment.parent_identity.name)


@assigned.register
def _(unassigned: NoneType) -> None:
    """A row nobody holds carries no assignment."""


@singledispatch
def carried(strike: StrikeTodo) -> StrikeTodo:
    """Every strike Core still declares keeps its kind."""
    return replace(strike, previous=assigned(strike.previous))


@carried.register
def _(strike: ClockStrike) -> StrikeTodo:
    return carried(StrikeTodo(**{item.name: getattr(strike, item.name)
                                 for item in fields(StrikeTodo)}))


def carried_todo(todo: Todo) -> Todo:
    return replace(
        todo, assignment=assigned(todo.assignment),
        strikes=tuple(map(carried, todo.strikes)),
        voided=tuple(replace(void, strike=carried(void.strike)) for void in todo.voided),
        released=tuple(replace(release, previous=assigned(release.previous))
                       for release in todo.released))


def carry(root: Path, *, apply: bool) -> int:
    path = TodoStore.path_in(root)
    if not path.exists():
        return 0
    with closing(CoordinationStore.writer(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            (dated,) = db.execute(
                "SELECT count(*) FROM pragma_table_info(?) WHERE name = 'deadline'",
                (Todo.declared_name,)).fetchone()
            if not dated:
                db.rollback()
                return 0
            db.execute(f'ALTER TABLE "{Todo.declared_name}" RENAME COLUMN deadline TO review_at')
            todos = tuple(map(carried_todo, Todo.stream(db)))
            Todo.drop(db)
            Todo.create(db)
            for todo in todos:
                todo.upsert(db)
            if apply:
                db.commit()
            else:
                db.rollback()
            return len(todos)
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
