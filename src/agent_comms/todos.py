"""A durable todo owns its one assignment; it does not start an agent.

This is an opt-in post-PR1 primitive. An ACP/fork adapter must verify current
thread identities and reserve a todo BEFORE spawning a worker. File claims,
collaboration edges and thread goals remain separate authorities.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from abc import abstractmethod

from .coordination_database import CoordinationStore
from .field_codec import FieldCodec
from .command import Command
from .declared_family import DeclaredFamily
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from .threads import Thread
from .typed_table import Column, Index, TypedRow, TypedTable
from .recorded_time import EventTime, UnrecordedEventTime


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
        return RecordedThreadIncarnation(self.owner, self.owner_created)


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
        return RecordedThreadIncarnation(self.owner, self.owner_created)

    @property
    def parent_identity(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.parent, self.parent_created)

    def owned_by(self, actor: ThreadIncarnation, generation: str | None) -> bool:
        return (self.owner_identity, self.generation) == (actor, generation)

    def require_successor(self, proposed: Assignment, current: Todo) -> None:
        if self.generation == proposed.generation:
            raise TodoConflict(current)


class TodoState(DeclaredFamily, affix="TodoState"):
    @classmethod
    def prepare_change(cls, goals, current):
        pass

    @classmethod
    def require_owner(cls, current: Todo) -> None:
        if current.assignment is None:
            raise TodoError("An open problem requires its owner and order at creation.")

    @classmethod
    def assign(cls, current: Todo, proposed: Assignment) -> Todo:
        raise TodoConflict(current)

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
        return current.assigned(proposed)


class BlockedTodoState(TodoState):
    pass


class DoneTodoState(TodoState):
    @classmethod
    def prepare_change(cls, goals, current):
        assignment = current.assignment
        goal = goals.registry.require(assignment.owner).require_goal(assignment.generation)
        goal.state.require_order_slot()
        goal.order.require_close()

    @classmethod
    def require_owner(cls, current):
        pass

    @classmethod
    def change(cls, current, command):
        raise TodoConflict(current)

    @classmethod
    def decide(cls, current, command):
        current.require_commander(command.actor)
        return replace(current, state=cls, assignment=None, revision=current.revision + 1)


@dataclass(frozen=True)
class TodoOperation(Command, DeclaredFamily, affix="TodoOperation"):
    expected_revision: int

    def __post_init__(self) -> None:
        if FieldCodec.decode(int, self.expected_revision) <= 0:
            raise TodoError("Todo operation requires a positive exact revision")

    def prepare(self, goals, current):
        pass

    def publish_order(self, goals, current):
        pass

    def record_activity(self, current, at):
        return replace(current, commanded_at=at)

    @abstractmethod
    def apply(self, current: Todo) -> Todo: ...


@dataclass(frozen=True)
class AssignTodo(TodoOperation):
    proposed: Assignment
    deadline: EventTime

    def apply(self, current):
        self.deadline.require_deadline()
        if current.assignment == self.proposed:
            current.require_result(replace(current, deadline=self.deadline))
            return current  # The original assignment generation is already reserved.
        current.require_revision(self.expected_revision)
        current.require_commander(self.proposed.parent_identity)
        return replace(current.state.assign(current, self.proposed), deadline=self.deadline)


@dataclass(frozen=True)
class StrikeTodo(TodoOperation):
    actor: ThreadIncarnation
    previous: Assignment
    reason: str

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        current.require_assignment(self.previous)
        _text(self.reason, "Strike reason", 2000)
        struck = replace(current, strikes=(*current.strikes, self), revision=current.revision + 1)
        return self.after_strike(current, struck)

    def after_strike(self, current, struck):
        if struck.strike_count >= 2:
            raise TodoError("A second strike requires an atomic reassignment, not an unowned problem.")
        return struck


@dataclass(frozen=True)
class ReassignStrikeTodo(StrikeTodo):
    proposed: Assignment
    deadline: EventTime

    def prepare(self, goals, current):
        goals.registry.require(self.proposed.owner).require_order_slot(self.proposed.generation, current.id)

    def after_strike(self, current, struck):
        if struck.strike_count < 2 or self.proposed.owner_identity == self.previous.owner_identity:
            raise TodoError("The second strike takes this problem away from its failed owner.")
        current.require_commander(self.proposed.parent_identity)
        self.deadline.require_deadline()
        return replace(current.assigned(self.proposed), strikes=struck.strikes, deadline=self.deadline)

    def publish_order(self, goals, current):
        from .goal_actions import ClearGoalAction, GoalPrecondition, OrderGoalAction

        order = goals.registry.require(self.previous.owner).require_goal(self.previous.generation).order
        goals.update_goal(self.proposed.owner, OrderGoalAction(
            order=order, goal_id=self.proposed.generation,
            expected_revision=current.revision, deadline=self.deadline, commander=self.proposed.parent))
        goals.update_goal(self.previous.owner, ClearGoalAction(
            expect=GoalPrecondition(goal_id=self.previous.generation)))


@dataclass(frozen=True)
class SweepTodo(TodoOperation):
    actor: ThreadIncarnation

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        return replace(current, revision=current.revision + 1)

    def record_activity(self, current, at):
        return replace(current, swept_at=at)


@dataclass(frozen=True)
class ChangeTodoState(TodoOperation):
    state: type[TodoState]
    actor: ThreadIncarnation
    generation: str | None

    def prepare(self, goals, current):
        self.state.prepare_change(goals, current)

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
    state: type[TodoState]
    revision: int = field(metadata={"sql": Column(check="revision>0")})
    goal: GoalRef | None
    assignment: Assignment | None
    deadline: EventTime = field(default_factory=UnrecordedEventTime)
    strikes: tuple[StrikeTodo, ...] = ()
    commanded_at: EventTime = field(default_factory=UnrecordedEventTime)
    swept_at: EventTime = field(default_factory=UnrecordedEventTime)
    commanded_stamp: float | None = field(default=None, init=False, compare=False,
        metadata={"sql": Column(generated="json_extract(commanded_at,'$.at')")})
    indexes = (Index(("creator", "creator_created", "commanded_stamp")), Index(("state",)))

    def __post_init__(self) -> None:
        self.state.require_owner(self)

    @property
    def creator_identity(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.creator, self.creator_created)

    @property
    def strike_count(self) -> int:
        return self.strikes_for(self.assignment.owner_identity) if self.assignment else 0

    def strikes_for(self, owner: ThreadIncarnation) -> int:
        return sum(strike.previous.owner_identity == owner for strike in self.strikes)

    def require_not_fired(self) -> None:
        if self.strike_count >= 2:
            raise TodoError("Two strikes take the problem away; reassign it before running work.")

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

    def assigned(self, proposed: Assignment) -> Todo:
        self.require_commander(proposed.parent_identity)
        if self.strikes_for(proposed.owner_identity) >= 2:
            raise TodoError("This owner has two strikes on the problem; choose another owner.")
        if self.assignment is not None:
            self.assignment.require_successor(proposed, self)
        return replace(self, assignment=proposed, revision=self.revision + 1)

    def require_released_order(self, owner: ThreadIncarnation, goal_id: str) -> None:
        if self.assignment is not None and self.assignment.owned_by(owner, goal_id):
            raise TodoConflict(self)

    def require_commander(self, actor: ThreadIncarnation) -> None:
        if self.creator_identity != actor:
            raise TodoConflict(self)

    def require_actor(self, actor: ThreadIncarnation, generation: str | None) -> None:
        if self.creator_identity != actor:
            self.require_assignee(actor, generation)

    def require_same_creation(self, proposed: Todo) -> None:
        self.require_result(
            replace(
                self,
                id=proposed.id,
                repo=proposed.repo,
                text=proposed.text,
                creator=proposed.creator,
                creator_created=proposed.creator_created,
                goal=proposed.goal,
                deadline=proposed.deadline,
            )
        )


@dataclass(frozen=True)
class TodoReference(TypedRow):
    id: str


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

    Assignment identity and revision deduplicate known operations. An uncertain
    input is not permission to replay it; inspect its original retained result.
    This store never launches a child.
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
        db = CoordinationStore.writer(self.path, isolation_level=None)
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

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

    def unowned(self) -> tuple[str, ...]:
        state, = Todo._fields_named(("state",))
        with closing(self._connect()) as db:
            return tuple(row.id for row in TodoReference.read(db.execute(
                f'SELECT id FROM "{Todo.declared_name}" WHERE assignment IS NULL AND state<>?',
                (state.encode(DoneTodoState),))))

    def commander_activity(self, creator: ThreadIncarnation) -> EventTime:
        with closing(self._connect()) as db:
            row, = Todo.read(db.execute(f'SELECT * FROM "{Todo.declared_name}" '
                'WHERE creator=? AND creator_created=? ORDER BY commanded_stamp DESC LIMIT 1',
                (creator.name, creator.created_at)))
            return row.commanded_at

    def open_problems(self) -> tuple[Todo, ...]:
        state, = Todo._fields_named(("state",))
        with closing(self._connect()) as db:
            return tuple(Todo.select(db, where="state<>? AND assignment IS NOT NULL",
                                     parameters=(state.encode(DoneTodoState),)))

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
        owner: Thread,
        generation: str,
        deadline: EventTime,
        goal: GoalRef | None = None,
    ) -> Todo:
        _text(todo_id, "Todo ID", 128)
        _text(text, "Todo text", 2000)
        repo_id = _repo(repo)
        identity, assignee = _participant(creator), _thread(owner)
        deadline.require_deadline()
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
            assignment=Assignment(assignee.name, assignee.created_at,
                                  identity.name, identity.created_at, generation),
            deadline=deadline,
            commanded_at=EventTime.capture(time.time()),
        )
        with self._write() as db:
            proposed.insert_if_absent(db)
            current = self._current(db, todo_id)
            current.require_same_creation(proposed)
            current.require_assignment(proposed.assignment)
            return current

    def apply(self, todo_id: str, command: TodoOperation) -> Todo:
        with self._write() as db:
            current = self._current(db, todo_id)
            result = command.apply(current)
            if result == current:
                return current
            command.record_activity(result, EventTime.capture(time.time())).upsert(db)
            return self._current(db, todo_id)

    def assign(
        self,
        todo_id: str,
        *,
        expected_revision: int,
        owner: Thread,
        parent: Thread,
        generation: str,
        deadline: EventTime,
    ) -> Todo:
        identity, parent_identity = _thread(owner), _thread(parent)
        proposed = Assignment(
            identity.name,
            identity.created_at,
            parent_identity.name,
            parent_identity.created_at,
            generation,
        )
        return self.apply(todo_id, AssignTodo(expected_revision, proposed, deadline))
