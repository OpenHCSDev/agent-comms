"""Pending problems rejoin their owner's Goal, which reopens (P59).

Before P48 an order joined the Goal it named even after that Goal completed, and a
refused send reserved its Todo under a Goal that was never published. Each pending
Todo is reattached to its owner's current Goal, and a terminal Goal holding one is
reopened, so every pending Todo belongs to an open Goal as Core now requires.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path

from cutover.carry import Carried, Carry

from agent_comms.coordination_database import CoordinationStore
from agent_comms.goal_states import ActiveGoal
from agent_comms.goals import Goal
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import _store_lock
from agent_comms.todos import Todo, TodoStore


class ProblemGoals(Carry):
    """Every pending Todo is assigned under its owner's current Goal, and that Goal is open."""

    after = "carry_todo_voids"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path, registry = TodoStore.path_in(root), root / "registry.json"
        if not path.exists():
            return Carried(0)
        with _store_lock(registry), closing(CoordinationStore.writer(path)) as db:
            store = RegistryStore(registry)
            store.private_guard_unlocked()
            document = store._read_unlocked().copy()
            db.execute("BEGIN IMMEDIATE")
            try:
                todos: list[Todo] = []
                reopened: dict[str, Goal] = {}
                for todo in Todo.stream(db):
                    if not todo.pending or todo.assignment is None:
                        continue
                    name = todo.assignment.owner
                    thread = document.threads.get(name)
                    goal = None if thread is None else thread.goal
                    owner = None if thread is None else thread.incarnation
                    if owner != todo.assignment.owner_identity or goal is None:
                        raise ValueError(f"{todo.id} is pending for an owner with no Goal")
                    if goal.state.terminal:
                        reopened[name] = replace(
                            goal, state=ActiveGoal(), revision=goal.revision + 1
                        )
                    if todo.assignment.generation != goal.id:
                        assignment = replace(todo.assignment, generation=goal.id)
                        todos.append(
                            replace(todo, assignment=assignment, revision=todo.revision + 1)
                        )
                for name, goal in reopened.items():
                    document.threads[name] = replace(document.threads[name], goal=goal)
                if apply:
                    for todo in todos:
                        todo.upsert(db)
                    db.commit()
                    if reopened:
                        store.save_unlocked(document)
                else:
                    db.rollback()
            except BaseException:
                db.rollback()
                raise
        changed = len(todos) + len(reopened)
        rewritten = (
            (
                *(item.name for item in root.glob(f"{path.name}*")),
                *((registry.name, store.guard_path.name) if reopened else ()),
            )
            if changed and apply
            else ()
        )
        return Carried(changed, {"reattached": len(todos), "reopened": len(reopened)}, rewritten)


if __name__ == "__main__":
    ProblemGoals.main()
