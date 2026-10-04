"""A durable todo owns its one assignment; it does not start an agent.

This is an opt-in post-PR1 primitive. An ACP/fork adapter must verify current
thread identities and reserve a todo BEFORE spawning a worker. File claims,
collaboration edges and thread goals remain separate authorities.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from abc import abstractmethod

from .field_codec import FieldCodec
from .command import Command
from .declared_family import DeclaredFamily
from .thread_identity import ThreadIncarnation
from .threads import Thread
from .typed_table import Column, TypedRow, TypedTable, sql_literal


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
        _text(self.owner, "Thread name", 128)
        try:
            self.incarnation.require_recorded()
        except ValueError as error:
            raise TodoError("Goal reference requires its recorded owner incarnation") from error
        _text(self.goal_id, "Goal ID", 128)

    @property
    def incarnation(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.owner, self.owner_created)


@dataclass(frozen=True, slots=True)
class Assignment:
    owner: str
    owner_created: float
    parent: str
    parent_created: float
    generation: str

    def __post_init__(self) -> None:
        _text(self.owner, "Thread name", 128)
        _text(self.parent, "Thread name", 128)
        try:
            self.owner_identity.require_recorded()
            self.parent_identity.require_recorded()
        except ValueError as error:
            raise TodoError("Assignment requires recorded owner and parent incarnations") from error
        _text(self.generation, "Assignment generation", 128)

    @property
    def owner_identity(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.owner, self.owner_created)

    @property
    def parent_identity(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.parent, self.parent_created)

    def owned_by(self, actor: ThreadIncarnation, generation: str | None) -> bool:
        return (self.owner_identity, self.generation) == (actor, generation)

    def require_successor(self, proposed: Assignment, current: Todo) -> None:
        if self.generation == proposed.generation:
            raise TodoConflict(current)


class TodoState(DeclaredFamily, affix="TodoState"):
    @classmethod
    def assign(cls, current: Todo, proposed: Assignment) -> Todo:
        raise TodoConflict(current)

    @classmethod
    def transfer(cls, current: Todo, command: TransferTodoChange) -> Todo:
        raise TodoConflict(current)

    @classmethod
    def transferred(cls, current: Todo, command: TransferTodoChange) -> Todo:
        raise TodoConflict(current)

    @classmethod
    def release(cls, current: Todo, command: ReleaseTodoChange) -> Todo:
        return replace(
            current,
            assignment=None,
            revision=current.revision + 1,
            last_transition=type(command),
            last_previous=command.previous,
        )

    @classmethod
    def released(cls, current: Todo, command: ReleaseTodoChange) -> Todo:
        current.require_result(
            replace(
                current,
                assignment=None,
                last_transition=type(command),
                last_previous=command.previous,
            )
        )
        return current

    @classmethod
    def change(cls, current: Todo, command: ChangeTodoState) -> Todo:
        return command.state.decide(current, command)

    @classmethod
    def decide(cls, current: Todo, command: ChangeTodoState) -> Todo:
        current.require_actor(command.actor, command.generation)
        return (
            current
            if cls is current.state
            else replace(current, state=cls, revision=current.revision + 1)
        )


class OpenTodoState(TodoState):
    @classmethod
    def assign(cls, current, proposed):
        current.require_assignment(None)
        return replace(current, assignment=proposed, revision=current.revision + 1)

    @classmethod
    def transfer(cls, current, command):
        current.require_assignment(command.previous)
        return replace(
            current,
            assignment=command.proposed,
            revision=current.revision + 1,
            last_transition=type(command),
            last_previous=command.previous,
        )

    @classmethod
    def transferred(cls, current, command):
        current.require_result(
            replace(
                current,
                assignment=command.proposed,
                last_transition=type(command),
                last_previous=command.previous,
            )
        )
        return current


class BlockedTodoState(TodoState):
    pass


class DoneTodoState(TodoState):
    @classmethod
    def release(cls, current, command):
        raise TodoConflict(current)

    @classmethod
    def released(cls, current, command):
        raise TodoConflict(current)

    @classmethod
    def change(cls, current, command):
        raise TodoConflict(current)

    @classmethod
    def decide(cls, current, command):
        if current.assignment is None:
            current.require_actor(command.actor, command.generation)
        else:
            current.require_assignee(command.actor, command.generation)
        return replace(current, state=cls, assignment=None, revision=current.revision + 1)


@dataclass(frozen=True)
class TodoOperation(Command):
    expected_revision: int

    def __post_init__(self) -> None:
        if FieldCodec.decode(int, self.expected_revision) <= 0:
            raise TodoError("Todo operation requires a positive exact revision")

    @abstractmethod
    def apply(self, current: Todo) -> Todo: ...


@dataclass(frozen=True)
class AssignTodo(TodoOperation):
    proposed: Assignment

    def apply(self, current):
        if current.assignment == self.proposed:
            return current  # The original assignment generation is already reserved.
        current.require_revision(self.expected_revision)
        return current.state.assign(current, self.proposed)


class AssignmentChange(TodoOperation, DeclaredFamily, affix="TodoChange"):
    """The durable row retains the operation kind and exact previous assignment."""

    previous: Assignment

    def __post_init__(self) -> None:
        super().__post_init__()
        if type(self.previous) is not Assignment:
            raise TodoError("Assignment change requires the exact previous assignment")


@dataclass(frozen=True)
class TransferTodoChange(AssignmentChange):
    previous: Assignment
    proposed: Assignment

    def apply(self, current):
        self.previous.require_successor(self.proposed, current)
        if current.revision == self.expected_revision + 1:
            return current.state.transferred(current, self)
        current.require_revision(self.expected_revision)
        return current.state.transfer(current, self)


@dataclass(frozen=True)
class ReleaseTodoChange(AssignmentChange):
    previous: Assignment

    def apply(self, current):
        if current.revision == self.expected_revision + 1:
            return current.state.released(current, self)
        current.require_revision(self.expected_revision)
        current.require_assignment(self.previous)
        return current.state.release(current, self)


@dataclass(frozen=True)
class ChangeTodoState(TodoOperation):
    state: type[TodoState]
    actor: ThreadIncarnation
    generation: str | None

    def apply(self, current):
        current.require_revision(self.expected_revision)
        return current.state.change(current, self)


@dataclass(frozen=True, slots=True)
class Todo(TypedTable):
    id: str = field(metadata={"sql": Column(primary_key=True)})
    repo: str
    text: str
    creator: str
    creator_created: float
    state: type[TodoState] = field(
        metadata={
            "sql": Column(
                check=(
                    '"state" IN ('
                    + ", ".join(
                        sql_literal(member)
                        for member in TodoState.members_with(TodoState)
                    )
                    + ")"
                )
            )
        }
    )
    revision: int = field(metadata={"sql": Column(check="revision>0")})
    goal: GoalRef | None
    assignment: Assignment | None
    last_transition: type[AssignmentChange] | None = field(
        default=None,
        metadata={
            "sql": Column(
                check=(
                    '"last_transition" IN ('
                    + ", ".join(
                        sql_literal(member)
                        for member in AssignmentChange.members_with(AssignmentChange)
                    )
                    + ")"
                )
            )
        },
    )
    last_previous: Assignment | None = None

    @property
    def creator_identity(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.creator, self.creator_created)

    def require_result(self, result: Todo) -> None:
        if self != result:
            raise TodoConflict(self)

    def require_revision(self, expected: int) -> None:
        if self.revision != expected:
            raise TodoConflict(self)

    def require_assignment(self, expected: Assignment | None) -> None:
        if self.assignment != expected:
            raise TodoConflict(self)

    def require_assignee(self, actor: ThreadIncarnation, generation: str | None) -> None:
        if self.assignment is None or not self.assignment.owned_by(actor, generation):
            raise TodoConflict(self)

    def require_actor(self, actor: ThreadIncarnation, generation: str | None) -> None:
        if self.creator_identity != actor:
            self.require_assignee(actor, generation)

    def require_same_creation(self, proposed: Todo) -> None:
        self.require_result(
            replace(
                proposed,
                state=self.state,
                revision=self.revision,
                assignment=self.assignment,
                last_transition=self.last_transition,
                last_previous=self.last_previous,
            )
        )


@dataclass(frozen=True)
class _TodoTable(TypedRow):
    name: str


def _text(value: str, label: str, limit: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise TodoError(f"{label} must be nonempty and at most {limit} characters.")
    return value


def _thread(thread: Thread) -> ThreadIncarnation:
    if type(thread) is not Thread or not thread.role.executable:
        raise TodoError("Assignments require an executable registered thread snapshot.")
    return _participant(thread)


def _participant(thread: Thread) -> ThreadIncarnation:
    if type(thread) is not Thread:
        raise TodoError("Creator must be a registered thread snapshot.")
    _text(thread.name, "Thread name", 128)
    try:
        thread.incarnation.require_recorded()
    except ValueError as error:
        raise TodoError("Creator requires a recorded incarnation") from error
    return thread.incarnation


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
            elif tables != [_TodoTable(FieldCodec.encode(Todo))]:
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
        identity = _participant(creator)
        if goal is not None and type(goal) is not GoalRef:
            raise TodoError("Goal reference must be a typed GoalRef.")
        proposed = Todo(
            id=todo_id,
            repo=repo_id,
            text=text,
            creator=identity.name,
            creator_created=identity.created_at,
            state=OpenTodoState,
            revision=1,
            goal=goal,
            assignment=None,
        )
        with self._write() as db:
            current = Todo.one(db, id=todo_id)
            if current is not None:
                current.require_same_creation(proposed)
                return current
            proposed.insert(db)
            return self._current(db, todo_id)

    def _apply(self, todo_id: str, command: TodoOperation) -> Todo:
        with self._write() as db:
            current = self._current(db, todo_id)
            result = command.apply(current)
            if result == current:
                return current
            changes = {
                item.name: getattr(result, item.name)
                for item in fields(Todo)
                if getattr(result, item.name) != getattr(current, item.name)
            }
            Todo.update(
                db, where="id=? AND revision=?", parameters=(todo_id, current.revision), **changes
            )
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
        identity, parent_identity = _thread(owner), _thread(parent)
        proposed = Assignment(
            identity.name,
            identity.created_at,
            parent_identity.name,
            parent_identity.created_at,
            generation,
        )
        return self._apply(todo_id, AssignTodo(expected_revision, proposed))

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
        identity, parent_identity = _thread(owner), _thread(parent)
        proposed = Assignment(
            identity.name,
            identity.created_at,
            parent_identity.name,
            parent_identity.created_at,
            generation,
        )
        return self._apply(todo_id, TransferTodoChange(expected_revision, previous, proposed))

    def release(self, todo_id: str, *, expected_revision: int, previous: Assignment) -> Todo:
        return self._apply(todo_id, ReleaseTodoChange(expected_revision, previous))

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
            state = FieldCodec.decode(type[TodoState], state)
        except ValueError as error:
            raise TodoError("Unknown todo state.") from error
        return self._apply(
            todo_id, ChangeTodoState(expected_revision, state, _participant(actor), generation)
        )
