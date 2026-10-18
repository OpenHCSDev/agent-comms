"""A durable todo owns its one assignment; it does not start an agent.

This is an opt-in post-PR1 primitive. An ACP/fork adapter must verify current
thread identities and reserve a todo BEFORE spawning a worker. File claims,
collaboration edges and thread goals remain separate authorities.
"""

from __future__ import annotations

import sqlite3
import time
import asyncio
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from abc import abstractmethod
from typing import ClassVar

from .coordination_database import CoordinationStore
from .field_codec import FieldCodec
from .command import Command
from .declared_family import DeclaredFamily
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from .threads import Thread
from .typed_table import Column, TypedRow, TypedTable
from .recorded_time import EventTime, UnrecordedEventTime
from .change_report import FileLine
from .goals import OrderCheck
from .child_process import BoundedRun, ExitedOutcome


@dataclass(frozen=True, kw_only=True)
class OrderDelivery(DeclaredFamily, affix="Delivery"):
    at: EventTime = field(default_factory=UnrecordedEventTime)

    async def run(self, check, *command):
        result = await BoundedRun.run(tuple(command), cwd=check.directory, timeout=120)
        if result.outcome != ExitedOutcome(0):
            raise ValueError(result.stderr.decode(errors="replace") or "Delivery source check failed.")
        return result.stdout.decode(errors="replace")

    async def verified(self, check):
        await self.require_source(check)
        result = await BoundedRun.run(("bash", "-c", check.command), cwd=check.directory, timeout=120)
        check.result.require(result)
        return replace(self, at=await self.delivery_time(check))

    @abstractmethod
    async def require_source(self, check): ...

    @abstractmethod
    async def delivery_time(self, check): ...


@dataclass(frozen=True, kw_only=True)
class CommitDelivery(OrderDelivery):
    commit: str

    def __post_init__(self):
        if not 7 <= len(self.commit) <= 40 or self.commit.strip("0123456789abcdef"):
            raise ValueError("Delivery requires a commit hash.")

    async def require_source(self, check):
        head = (await self.run(check, "git", "rev-parse", "HEAD", f"{self.commit}^{{commit}}")).split()
        dirty = await self.run(check, "git", "status", "--porcelain", "--untracked-files=no")
        if len(head) != 2 or head[0] != head[1] or dirty.strip():
            raise ValueError("Run the order check on its clean delivered commit.")

    async def delivery_time(self, check):
        return EventTime.capture(float(await self.run(check, "git", "show", "-s", "--format=%ct", self.commit)))


@dataclass(frozen=True, kw_only=True)
class OperationDelivery(OrderDelivery):
    async def require_source(self, check):
        pass

    async def delivery_time(self, check):
        return EventTime.capture(time.time())


@dataclass(frozen=True)
class FirstCall(DeclaredFamily, affix="FirstCall"):
    def observe(self):
        return self

    def notify(self):
        return self


class AwaitingFirstCall(FirstCall):
    def observe(self):
        return ObservedFirstCall()

    def notify(self):
        return NotifiedFirstCall()


class ObservedFirstCall(FirstCall):
    pass


class NotifiedFirstCall(FirstCall):
    def observe(self):
        return LateObservedFirstCall()


class LateObservedFirstCall(ObservedFirstCall, NotifiedFirstCall):
    pass


class FirstCallEvent(DeclaredFamily, affix="FirstCallEvent"):
    @classmethod
    @abstractmethod
    def apply(cls, current): ...


class ObservedFirstCallEvent(FirstCallEvent):
    @classmethod
    def apply(cls, current):
        return current.observe()


class NotifiedFirstCallEvent(FirstCallEvent):
    @classmethod
    def apply(cls, current):
        return current.notify()


@dataclass(frozen=True, kw_only=True)
class ProblemOrder(DeclaredFamily, affix="ProblemOrder"):
    """One checked order lives on its Todo, independently of the unit's Goal."""

    id: str
    owner: FileLine
    check: OrderCheck
    edits: tuple[str, ...]
    banned: tuple[str, ...]
    report: str
    at: EventTime
    first_call_deadline: EventTime
    first_call: FirstCall = field(default_factory=AwaitingFirstCall)

    def __post_init__(self):
        if not self.edits or not self.banned:
            raise ValueError("An order requires edits and banned actions.")
        if any(not text.strip() for text in (self.id, self.report, *self.edits, *self.banned)):
            raise ValueError("Order text cannot be empty.")
        self.at.require_recorded("An order")
        self.first_call_deadline.require_deadline()

    pending: ClassVar[bool]

    def copy_as(self, kind, **evidence):
        return kind(**{item.name: getattr(self, item.name) for item in fields(self)}, **evidence)

    def instructions(self, todo):
        return "\n".join((f"Problem: {todo.id}: {todo.text}", f"Order: {self.id}",
            f"Owner: {self.owner.to_wire()}",
            f"Check: {self.check.directory} :: {self.check.command}",
            f"Deadline: {FieldCodec.encode(todo.deadline)}",
            *(f"{n}. {edit}" for n, edit in enumerate(self.edits, 1)),
            "Banned: " + "; ".join(self.banned), "Report: " + self.report))

    def delivered(self, delivery):
        delivery.at.require_delivery()
        return self.copy_as(DeliveredProblemOrder, delivery=delivery)

    def installed(self, works, artifact):
        raise ValueError("Check the delivered order before recording installation.")

    def require_close(self):
        raise ValueError("Closing requires recorded installed application evidence.")


@dataclass(frozen=True, kw_only=True)
class PendingProblemOrder(ProblemOrder):
    pending: ClassVar[bool] = True


@dataclass(frozen=True, kw_only=True)
class DeliveredProblemOrder(ProblemOrder):
    delivery: OrderDelivery
    pending: ClassVar[bool] = False

    def delivered(self, delivery):
        if delivery != self.delivery:
            raise ValueError("The order already records a different delivery.")
        return self

    def installed(self, works, artifact):
        return self.copy_as(InstalledProblemOrder, works=works, artifact=artifact)


@dataclass(frozen=True, kw_only=True)
class InstalledProblemOrder(DeliveredProblemOrder):
    works: str
    artifact: str

    def __post_init__(self):
        super().__post_init__()
        if len(self.works.split()) < 8 or not self.artifact.strip():
            raise ValueError("Describe what worked (8+ words) and name its artifact file.")

    def installed(self, works, artifact):
        if (works, artifact) != (self.works, self.artifact):
            raise ValueError("The order already records different installed evidence.")
        return self

    def require_close(self):
        pass


@dataclass(frozen=True)
class Hunt(DeclaredFamily, affix="Hunt"):
    where: FileLine
    why: str
    at: EventTime

    def __post_init__(self):
        if len(self.why.split()) < 10:
            raise ValueError("Describe the inspected code and verdict in ten or more words.")
        self.at.require_recorded("A hunt")

    def require_order(self, order):
        pass


@dataclass(frozen=True)
class CleanHunt(Hunt):
    pass


@dataclass(frozen=True)
class OrderedHunt(Hunt):
    sent: str

    def require_order(self, order):
        if self.sent != order.id:
            raise ValueError("The hunt must name this problem's current order.")


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
    open: ClassVar[bool] = True  # still on the board: assigned and not closed with installed evidence

    @classmethod
    def prepare_change(cls, goals, current):
        pass

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
    open: ClassVar[bool] = False

    @classmethod
    def prepare_change(cls, goals, current):
        current.order.require_close()

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
        return self

    @abstractmethod
    def apply(self, current: Todo) -> Todo: ...


@dataclass(frozen=True)
class AssignTodo(TodoOperation):
    proposed: Assignment
    deadline: EventTime
    order: PendingProblemOrder

    def apply(self, current):
        self.deadline.require_deadline()
        if (current.assignment, current.deadline, current.order) == (self.proposed, self.deadline, self.order):
            return current
        current.require_revision(self.expected_revision)
        current.require_commander(self.proposed.parent_identity)
        assigned = current if current.assignment == self.proposed else current.state.assign(current, self.proposed)
        return replace(assigned, deadline=self.deadline, order=self.order, revision=current.revision + 1)


@dataclass(frozen=True)
class StrikeTodo(TodoOperation):
    actor: ThreadIncarnation
    previous: Assignment
    reason: str
    at: EventTime = field(default_factory=lambda: EventTime.capture(time.time()))

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        current.require_assignment(self.previous)
        _text(self.reason, "Strike reason", 2000)
        return replace(current, strikes=(*current.strikes, self), revision=current.revision + 1)


@dataclass(frozen=True)
class RecordFirstCallTodo(TodoOperation):
    actor: ThreadIncarnation
    event: type[FirstCallEvent]

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        order = replace(current.order, first_call=self.event.apply(current.order.first_call))
        return current if order == current.order else replace(current, order=order, revision=current.revision + 1)


@dataclass(frozen=True)
class RecordHuntTodo(TodoOperation):
    actor: ThreadIncarnation
    hunt: Hunt

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        self.hunt.where.require_exists()
        self.hunt.require_order(current.order)
        return replace(current, hunts=(*current.hunts, self.hunt), revision=current.revision + 1)


@dataclass(frozen=True)
class DeliverTodo(TodoOperation):
    actor: ThreadIncarnation
    delivery: OrderDelivery

    def prepare(self, goals, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        return replace(self, delivery=asyncio.run(self.delivery.verified(current.order.check)))

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        return replace(current, order=current.order.delivered(self.delivery), revision=current.revision + 1)


@dataclass(frozen=True)
class InstallTodo(TodoOperation):
    actor: ThreadIncarnation
    works: str
    artifact: str

    def apply(self, current):
        current.require_revision(self.expected_revision)
        current.require_commander(self.actor)
        if not Path(self.artifact).is_file():
            raise ValueError("Retain the installed application artifact before closing.")
        installed = replace(current, order=current.order.installed(self.works, self.artifact))
        return DoneTodoState.decide(installed, ChangeTodoState(
            current.revision, DoneTodoState, self.actor, current.assignment.generation))


@dataclass(frozen=True)
class ChangeTodoState(TodoOperation):
    state: type[TodoState]
    actor: ThreadIncarnation
    generation: str | None

    def prepare(self, goals, current):
        self.state.prepare_change(goals, current)
        return self

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
    order: ProblemOrder
    deadline: EventTime = field(default_factory=UnrecordedEventTime)
    strikes: tuple[StrikeTodo, ...] = ()
    hunts: tuple[Hunt, ...] = ()

    @property
    def pending(self):
        return self.order.pending

    @property
    def creator_identity(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.creator, self.creator_created)

    @property
    def strike_count(self) -> int:
        return sum(strike.previous == self.assignment for strike in self.strikes)

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
                order=proposed.order,
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

    def for_goal(self, owner: ThreadIncarnation, goal_id: str) -> tuple[Todo, ...]:
        with closing(self._connect()) as db:
            return tuple(Todo.select(db,
                where="json_extract(assignment,'$.owner')=? AND json_extract(assignment,'$.owner_created')=? AND json_extract(assignment,'$.generation')=?",
                parameters=(owner.name, owner.created_at, goal_id)))

    def for_commander(self, commander: ThreadIncarnation) -> tuple[Todo, ...]:
        with closing(self._connect()) as db:
            return tuple(Todo.select(db, where="creator=? AND creator_created=?",
                parameters=(commander.name, commander.created_at)))

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
        order: PendingProblemOrder,
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
            order=order,
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
            result.upsert(db)
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
        order: PendingProblemOrder,
    ) -> Todo:
        identity, parent_identity = _thread(owner), _thread(parent)
        proposed = Assignment(
            identity.name,
            identity.created_at,
            parent_identity.name,
            parent_identity.created_at,
            generation,
        )
        return self.apply(todo_id, AssignTodo(expected_revision, proposed, deadline, order))
