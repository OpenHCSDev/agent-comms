"""A durable todo owns its one assignment; it does not start an agent.

This is an opt-in post-PR1 primitive. An ACP/fork adapter must verify current
thread identities and reserve a todo BEFORE spawning a worker. File claims,
collaboration edges and thread goals remain separate authorities.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_type_hints

from .field_codec import FieldCodec
from .threads import Thread
from .typed_table import Column, TypedRow, TypedTable


class TodoError(ValueError):
    """A todo request is invalid or its persisted state cannot be trusted."""


class TodoConflict(TodoError):  # noqa: N818 - domain-specific conflict outcome
    """The expected revision, assignee or generation is no longer current."""

    def __init__(self, current: Todo):
        self.current = current
        owner = current.assignment.owner if current.assignment else "unassigned"
        super().__init__(f"Todo {current.id} changed (revision {current.revision}, owner {owner}).")


@dataclass(frozen=True, slots=True)
class GoalRef:
    owner: str
    owner_created: float
    goal_id: str

    def __post_init__(self) -> None:
        _identity(self.owner, self.owner_created)
        _text(self.goal_id, "Goal ID", 128)


@dataclass(frozen=True, slots=True)
class Assignment:
    owner: str
    owner_created: float
    parent: str
    parent_created: float
    generation: str

    def __post_init__(self) -> None:
        _identity(self.owner, self.owner_created)
        _identity(self.parent, self.parent_created)
        _text(self.generation, "Assignment generation", 128)


@dataclass(frozen=True, slots=True)
class Todo(TypedTable):
    id: str = field(metadata={"sql": Column(primary_key=True)})
    repo: str
    text: str
    creator: str
    creator_created: float
    state: Literal["open", "blocked", "done"]
    revision: int = field(metadata={"sql": Column(check="revision>0")})
    goal: GoalRef | None
    assignment: Assignment | None
    last_transition: Literal["transfer", "release"] | None = None
    last_previous: Assignment | None = None


@dataclass(frozen=True)
class _TodoTable(TypedRow):
    name: str


def _text(value: str, label: str, limit: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise TodoError(f"{label} must be nonempty and at most {limit} characters.")
    return value


def _identity(name: str, created: float) -> None:
    _text(name, "Thread name", 128)
    if type(created) not in (int, float) or not math.isfinite(created) or created <= 0:
        raise TodoError("Thread incarnation timestamp must be finite and positive.")


def _thread(thread: Thread) -> tuple[str, float]:
    if type(thread) is not Thread or not thread.role.executable:
        raise TodoError("Assignments require an executable registered thread snapshot.")
    _identity(thread.name, thread.created_at)
    return thread.name, thread.created_at


def _participant(thread: Thread) -> tuple[str, float]:
    if type(thread) is not Thread:
        raise TodoError("Creator must be a registered thread snapshot.")
    _identity(thread.name, thread.created_at)
    return thread.name, thread.created_at


def _repo(value: str) -> str:
    _text(value, "Repo ID", 200)
    pieces = value.split("/")
    if len(pieces) != 2 or any(
        piece in {"", ".", ".."} or not all(char.isalnum() or char in "-_." for char in piece)
        for piece in pieces
    ):
        raise TodoError("Repo ID must have the exact owner/repo form.")
    return value.lower()


class TodoStore:
    """One SQLite row owns both todo state and its exclusive assignment.

    A caller may retry the SAME create ID or assignment generation after an
    uncertain reply. A different assignee always receives the current owner,
    without entering a queue or launching another child.
    """

    def __init__(self, root: Path):
        directory = Path(root).resolve(strict=True)
        if not directory.is_dir():
            raise TodoError("Todo root must already exist.")
        self.path = directory / "todos.sqlite3"
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            tables = _TodoTable.read(
                db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            )
            if not tables:
                Todo.create(db)
            elif tables != [_TodoTable(Todo.declared_name)]:
                raise TodoError("Todo storage requires the one-shot durable migration.")
            # Decode exactly the current declared columns; no runtime converters.
            Todo.select(db, where="0")
            db.execute("COMMIT")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def _current(db: sqlite3.Connection, todo_id: str) -> Todo:
        row = Todo.one(db, id=todo_id)
        if row is None:
            raise TodoError(f"Unknown todo: {todo_id}.")
        return row

    def get(self, todo_id: str) -> Todo:
        _text(todo_id, "Todo ID", 128)
        with closing(self._connect()) as db:
            return self._current(db, todo_id)

    def list(self, repo: str) -> tuple[Todo, ...]:
        with closing(self._connect()) as db:
            return tuple(
                Todo.read(
                    db.execute(
                        f"SELECT * FROM {Todo.declared_name} WHERE repo=? ORDER BY rowid",
                        (_repo(repo),),
                    )
                )
            )

    def create(
        self,
        todo_id: str,
        repo: str,
        text: str,
        *,
        creator: Thread,
        goal: GoalRef | None = None,
    ) -> Todo:
        _text(todo_id, "Todo ID", 128)
        _text(text, "Todo text", 2000)
        repo_id = _repo(repo)
        name, created = _participant(creator)
        if goal is not None and type(goal) is not GoalRef:
            raise TodoError("Goal reference must be a typed GoalRef.")
        with self._write() as db:
            current = Todo.one(db, id=todo_id)
            if current is not None:
                if (
                    current.repo,
                    current.text,
                    current.creator,
                    current.creator_created,
                    current.goal,
                ) == (repo_id, text, name, created, goal):
                    return current  # Same request ID never creates a second todo.
                raise TodoConflict(current)
            Todo(
                id=todo_id,
                repo=repo_id,
                text=text,
                creator=name,
                creator_created=created,
                state="open",
                revision=1,
                goal=goal,
                assignment=None,
            ).insert(db)
            return self._current(db, todo_id)

    def assign(
        self,
        todo_id: str,
        *,
        expected_revision: int,
        owner: Thread,
        parent: Thread,
        generation: str,
    ) -> Todo:
        _text(generation, "Assignment generation", 128)
        name, created = _thread(owner)
        parent_name, parent_created = _thread(parent)
        proposed = Assignment(name, created, parent_name, parent_created, generation)
        with self._write() as db:
            current = self._current(db, todo_id)
            if current.assignment == proposed:
                return current  # Retry after a committed but uncertain response.
            if (
                current.revision != expected_revision
                or current.state != "open"
                or current.assignment is not None
            ):
                raise TodoConflict(current)
            Todo.update(
                db,
                where="id=?",
                parameters=(todo_id,),
                assignment=proposed,
                revision=current.revision + 1,
            )
            return self._current(db, todo_id)

    def transfer(
        self,
        todo_id: str,
        *,
        expected_revision: int,
        previous: Assignment,
        owner: Thread,
        parent: Thread,
        generation: str,
    ) -> Todo:
        """Move one current assignment without an unclaimed race window."""
        _text(generation, "Assignment generation", 128)
        name, created = _thread(owner)
        parent_name, parent_created = _thread(parent)
        proposed = Assignment(name, created, parent_name, parent_created, generation)
        if type(previous) is not Assignment:
            raise TodoError("Transfer requires the exact previous assignment.")
        with self._write() as db:
            current = self._current(db, todo_id)
            if (
                current.revision == expected_revision + 1
                and current.assignment == proposed
                and current.state == "open"
                and generation != previous.generation
                and current.last_transition == "transfer"
                and current.last_previous == previous
            ):
                return current  # Exact retry after a committed, uncertain response.
            if (
                current.revision != expected_revision
                or current.assignment != previous
                or current.state != "open"
                or generation == previous.generation
            ):
                raise TodoConflict(current)
            Todo.update(
                db,
                where="id=?",
                parameters=(todo_id,),
                assignment=proposed,
                revision=current.revision + 1,
                last_transition="transfer",
                last_previous=previous,
            )
            return self._current(db, todo_id)

    def release(self, todo_id: str, *, expected_revision: int, previous: Assignment) -> Todo:
        if type(previous) is not Assignment:
            raise TodoError("Release requires the exact previous assignment.")
        with self._write() as db:
            current = self._current(db, todo_id)
            if (
                current.revision == expected_revision + 1
                and current.assignment is None
                and current.state != "done"
                and current.last_transition == "release"
                and current.last_previous == previous
            ):
                return current  # Exact retry after a committed, uncertain response.
            if (
                current.revision != expected_revision
                or current.assignment != previous
                or current.state == "done"
            ):
                raise TodoConflict(current)
            Todo.update(
                db,
                where="id=?",
                parameters=(todo_id,),
                assignment=None,
                revision=current.revision + 1,
                last_transition="release",
                last_previous=previous,
            )
            return self._current(db, todo_id)

    def set_state(
        self,
        todo_id: str,
        *,
        expected_revision: int,
        state: str,
        actor: Thread,
        generation: str | None = None,
    ) -> Todo:
        """An explicit, revision-checked decision; never infer it from Pi output."""
        try:
            state = FieldCodec.decode(get_type_hints(Todo)["state"], state)
        except ValueError as error:
            raise TodoError("Unknown todo state.") from error
        actor_name, actor_created = _participant(actor)
        with self._write() as db:
            current = self._current(db, todo_id)
            if current.revision != expected_revision or current.state == "done":
                raise TodoConflict(current)
            is_creator = (actor_name, actor_created) == (current.creator, current.creator_created)
            is_assignee = (
                current.assignment is not None
                and generation is not None
                and (actor_name, actor_created, generation)
                == (
                    current.assignment.owner,
                    current.assignment.owner_created,
                    current.assignment.generation,
                )
            )
            if not (is_creator or is_assignee):
                raise TodoConflict(current)
            if state == "done" and current.assignment is not None and not is_assignee:
                raise TodoConflict(current)
            if state == current.state:
                return current
            Todo.update(
                db,
                where="id=?",
                parameters=(todo_id,),
                state=state,
                revision=current.revision + 1,
                assignment=None if state == "done" else current.assignment,
            )
            return self._current(db, todo_id)
