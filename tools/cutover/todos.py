"""One-shot durable todo staging for S12; remove after the quiet cutover.

The parent stops todo writers and installs the verified stage in place. This
command reads the retired database in a snapshot, writes only a new persistent
stage directory, and never changes or resets the source.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent_comms.field_codec import FieldCodec
from agent_comms.todos import Assignment, GoalRef, Todo, TodoStore
from agent_comms.typed_table import TypedRow


@dataclass(frozen=True)
class RetiredTodo(TypedRow):
    id: str
    repo: str
    text: str
    creator: str
    creator_created: float
    state: Literal["open", "blocked", "done"]
    revision: int
    goal_owner: str | None
    goal_created: float | None
    goal_id: str | None
    assignee: str | None
    assignee_created: float | None
    parent: str | None
    parent_created: float | None
    generation: str | None
    last_transition: Literal["transfer", "release"] | None
    last_previous: str | None

    def current(self) -> Todo:
        goal = None
        if (self.goal_owner, self.goal_created, self.goal_id) != (None, None, None):
            goal = GoalRef(self.goal_owner, self.goal_created, self.goal_id)
        assignment = None
        if (
            self.assignee,
            self.assignee_created,
            self.parent,
            self.parent_created,
            self.generation,
        ) != (None, None, None, None, None):
            assignment = Assignment(
                self.assignee,
                self.assignee_created,
                self.parent,
                self.parent_created,
                self.generation,
            )
        previous = None
        if self.last_previous is not None:
            previous = Assignment(
                *FieldCodec.decode(
                    tuple[str, float, str, float, str], json.loads(self.last_previous)
                )
            )
        return Todo(
            id=self.id,
            repo=self.repo,
            text=self.text,
            creator=self.creator,
            creator_created=self.creator_created,
            state=self.state,
            revision=self.revision,
            goal=goal,
            assignment=assignment,
            last_transition=self.last_transition,
            last_previous=previous,
        )


def stage_todos(source: Path, stage: Path) -> int:
    """Carry every todo, revision, goal, owner and uncertain-retry identity across."""
    source = source.resolve(strict=True)
    stage = stage.absolute()
    # A new directory makes the output entirely owned; never overwrite a stage.
    stage.mkdir(mode=0o700)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as old:
        old.execute("BEGIN")
        rows = [
            row.current()
            for row in RetiredTodo.read(old.execute("SELECT * FROM todos ORDER BY rowid"))
        ]
        target = TodoStore(stage)
        with target._write() as db:
            for row in rows:
                row.insert(db)
        # Reopen through the same current path used by production consumers.
        target = TodoStore(stage)
        with closing(target._connect()) as db:
            persisted = Todo.read(db.execute(f"SELECT * FROM {Todo.declared_name} ORDER BY rowid"))
        if persisted != rows:
            raise ValueError("Staged todo identities/revisions/retry evidence differ")
        return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="quiescent prior todos.sqlite3")
    parser.add_argument("stage", type=Path, help="new persistent staging directory")
    args = parser.parse_args()
    count = stage_todos(args.source, args.stage)
    print(f"Staged and reopened {count} todos; source unchanged: {args.stage / 'todos.sqlite3'}")


if __name__ == "__main__":
    main()
