"""A problem owns its definition and exclusive assignment; Goals own its orders."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from abc import abstractmethod
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, fields, replace
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING

from .agent_activity import AgentActivity
from .activity import ObservedActivity
from .child_process import BoundedRun
from .command import Command
from .coordination_database import CoordinationStore
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .fleet import UnitObservation
from .message_reference import MessageReference
from .goal_actions import (
    ClearGoalAction, CompletedGoalAction, GoalActionContext, GoalPrecondition,
    RuntimeInvocable, SetGoalAction,
)
from .goal_mentions import bind_goal_mentions
from .goals import Goal, GoalAuthor, GoalRef
from .recorded_time import RecordedEventTime
from .registration import Registration
from .registry_document import RegistrySnapshot
from .thread_identity import RecordedThreadIncarnation
from .typed_table import Column, TypedRow, TypedTable, sql_literal

if TYPE_CHECKING:
    from .threads import Thread
    from .goal_management import Goals
    from .goal_progress import GoalProgress
    from .goals import GoalDeadline
    from .message_bus import MessageBus


class TodoError(ValueError):
    """A request is invalid or its retained state cannot be trusted."""


class TodoConflict(TodoError):  # noqa: N818
    def __init__(self, current: Todo):
        self.current = current
        super().__init__(f"Todo {current.id} changed (revision {current.revision}, "
                         f"owner {current.assignment.description}).")


def _text(value: str, label: str, limit: int) -> str:
    if not value.strip() or len(value) > limit:
        raise TodoError(f"{label} must be nonempty and at most {limit} characters.")
    return value


class TodoWhere(DeclaredFamily, affix="TodoWhere"):
    @abstractmethod
    def require_source(self, bus: MessageBus) -> None: ...


@dataclass(frozen=True)
class SourceTodoWhere(TodoWhere):
    path: str
    line: int

    def __post_init__(self):
        _text(self.path, "Source path", 4096)
        if self.line < 1:
            raise TodoError("Source line must be positive.")

    def require_source(self, bus):
        with Path(self.path).open() as source:
            if not tuple(islice(source, self.line - 1, self.line)):
                raise TodoError(f"Source line does not exist: {self.path}:{self.line}")


@dataclass(frozen=True)
class MessageTodoWhere(TodoWhere):
    message: MessageReference

    def require_source(self, bus):
        with bus.log.certified_read() as source:
            if source.delivery(self.message.seq).message.reference != self.message:
                raise TodoError("Problem message reference changed.")


@dataclass(frozen=True)
class AcceptedCommit:
    sha: str
    committed_at: float


@dataclass(frozen=True)
class AcceptanceCheck:
    repo: str
    command: str
    expected_count: int
    timeout: float = 120.0

    def __post_init__(self):
        _text(self.repo, "Check checkout", 4096)
        _text(self.command, "Acceptance command", 8000)
        if self.expected_count < 0 or not 0 < self.timeout <= 600:
            raise TodoError("Check requires a nonnegative count and a timeout of at most 600s.")

    async def _run(self, *command: str) -> str:
        result = await BoundedRun.run(tuple(command), cwd=self.repo, timeout=self.timeout)
        if not result.outcome.successful:
            raise TodoError(f"Acceptance command failed: {result.outcome}; "
                            f"{result.stderr.decode(errors='replace')[-2000:]}")
        return result.stdout.decode(errors="strict").strip()

    async def _check(self, sha: str) -> AcceptedCommit:
        # The delivered commit, not the report's arrival, owns delivery time.
        async with asyncio.timeout(self.timeout):
            head = await self._run("git", "show", "-s", "--format=%H %ct", "HEAD")
            actual, timestamp = head.split()
            if not actual.startswith(sha.lower()):
                raise TodoError("Reported SHA is not the acceptance checkout's HEAD.")
            await self._run("git", "diff", "--exit-code", "HEAD", "--")
            output = await self._run("bash", "-c", self.command)
            if re.fullmatch(r"\d+", output) is None or int(output) != self.expected_count:
                raise TodoError(f"Acceptance expected {self.expected_count}; output: {output[-2000:]}")
            if await self._run("git", "show", "-s", "--format=%H %ct", "HEAD") != head:
                raise TodoError("Acceptance checkout changed while the check ran.")
            await self._run("git", "diff", "--exit-code", "HEAD", "--")
            return AcceptedCommit(actual, float(timestamp))

    def check(self, sha: str) -> AcceptedCommit:
        if re.fullmatch(r"[0-9a-fA-F]{7,64}", sha) is None:
            raise TodoError("Completion requires a reported commit SHA.")
        return asyncio.run(self._check(sha))


@dataclass(frozen=True, slots=True)
class Assignment:
    goal: GoalRef
    parent: RecordedThreadIncarnation

    def __post_init__(self):
        self.parent.require_recorded()
        _text(self.generation, "Assignment generation", 128)

    @property
    def owner(self) -> RecordedThreadIncarnation:
        return self.goal.owner

    @property
    def generation(self) -> str:
        # The assigned order's key owns both identities. Do not persist copies
        # of its owner or a second generated ID beside the GoalRef.
        return self.goal.goal_id

    def require_successor(self, proposed: Assignment, current: Todo):
        if self.generation == proposed.generation or self.owner.same_publication_birth(proposed.owner):
            raise TodoConflict(current)


class TodoAssignment(DeclaredFamily, affix="TodoAssignment"):
    @abstractmethod
    def observe(self, problem: Todo, snapshot: RegistrySnapshot,
                activities: Mapping[str, ObservedActivity], now: float) -> FleetEntry: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @abstractmethod
    def require_assigned(self, current: Todo) -> Assignment: ...

    @abstractmethod
    def assign(self, current: Todo, proposed: Assignment) -> Todo: ...

    def require_exact(self, current: Todo, expected: Assignment):
        if self.require_assigned(current) != expected:
            raise TodoConflict(current)


@dataclass(frozen=True)
class UnassignedTodoAssignment(TodoAssignment):
    def observe(self, problem, snapshot, activities, now):
        return UnassignedFleetEntry(todo=problem)

    @property
    def description(self):
        return "unassigned"

    def require_assigned(self, current):
        raise TodoConflict(current)

    def assign(self, current, proposed):
        return replace(current, assignment=AssignedTodoAssignment(proposed))


@dataclass(frozen=True)
class AssignedTodoAssignment(TodoAssignment):
    assignment: Assignment

    def observe(self, problem, snapshot, activities, now):
        goal = self.assignment.goal.current(snapshot)
        if goal is None:
            return FormerOrderFleetEntry(todo=problem)
        owner = snapshot.require(self.assignment.owner.name)
        return AssignedFleetEntry(todo=problem, goal=goal,
                                  activity=activities[owner.name], observed_at=now)

    @property
    def description(self):
        return self.assignment.owner.name

    def require_assigned(self, current):
        return self.assignment

    def assign(self, current, proposed):
        raise TodoConflict(current)


class TodoState(DeclaredFamily, affix="TodoState"):
    @classmethod
    def assign(cls, current: Todo, proposed: Assignment) -> Todo:
        raise TodoConflict(current)

    @classmethod
    def transfer(cls, current: Todo, command: TransferTodoChange) -> Todo:
        raise TodoConflict(current)

    @classmethod
    def release(cls, current: Todo, command: ReleaseTodoChange) -> Todo:
        current.assignment.require_exact(current, command.previous)
        return replace(current, assignment=UnassignedTodoAssignment())

    @classmethod
    def complete(cls, current: Todo) -> Todo:
        return replace(current, state=DoneTodoState)

    @classmethod
    def change(cls, current: Todo, command: ChangeTodoState) -> Todo:
        return command.state.decide(current, command)

    @classmethod
    def decide(cls, current: Todo, command: ChangeTodoState) -> Todo:
        current.require_actor(command.actor)
        return replace(current, state=cls)


class OpenTodoState(TodoState):
    @classmethod
    def assign(cls, current, proposed):
        return current.assignment.assign(current, proposed)

    @classmethod
    def transfer(cls, current, command):
        current.assignment.require_exact(current, command.previous)
        return replace(current, assignment=AssignedTodoAssignment(command.proposed))


class BlockedTodoState(TodoState):
    pass


class DoneTodoState(TodoState):
    @classmethod
    def complete(cls, current):
        return current

    @classmethod
    def release(cls, current, command):
        raise TodoConflict(current)

    @classmethod
    def change(cls, current, command):
        raise TodoConflict(current)

    @classmethod
    def decide(cls, current, command):
        raise TodoError("Use checked completion; a status change cannot close a problem.")


class TodoChangeRecord(DeclaredFamily, affix="TodoChangeRecord"):
    @abstractmethod
    def matches(self, command: TodoOperation) -> bool: ...


@dataclass(frozen=True)
class OriginalTodoChangeRecord(TodoChangeRecord):
    def matches(self, command):
        return False


@dataclass(frozen=True)
class AppliedTodoChangeRecord(TodoChangeRecord):
    command: TodoOperation

    def matches(self, command):
        return self.command == command


@dataclass(frozen=True)
class TodoOperation(Command, DeclaredFamily, affix="TodoOperation"):
    expected_revision: int

    def __post_init__(self):
        if self.expected_revision <= 0:
            raise TodoError("Todo operation requires a positive exact revision.")

    def apply(self, current: Todo) -> Todo:
        if current.last_change.matches(self):
            return current
        current.require_revision(self.expected_revision)
        result = self.decide(current)
        if result == current:
            return current
        return replace(result, revision=current.revision + 1,
                       last_change=AppliedTodoChangeRecord(self))

    @abstractmethod
    def decide(self, current: Todo) -> Todo: ...


@dataclass(frozen=True)
class AssignTodo(TodoOperation):
    proposed: Assignment

    def decide(self, current):
        current.require_actor(self.proposed.parent)
        return current.state.assign(current, self.proposed)


@dataclass(frozen=True)
class TransferTodoChange(TodoOperation):
    previous: Assignment
    proposed: Assignment

    def decide(self, current):
        current.require_actor(self.proposed.parent)
        self.previous.require_successor(self.proposed, current)
        return current.state.transfer(current, self)


@dataclass(frozen=True)
class ReleaseTodoChange(TodoOperation):
    previous: Assignment

    def decide(self, current):
        return current.state.release(current, self)


@dataclass(frozen=True)
class ChangeTodoState(TodoOperation):
    state: type[TodoState]
    actor: RecordedThreadIncarnation

    def decide(self, current):
        return current.state.change(current, self)


@dataclass(frozen=True)
class CompleteTodo(TodoOperation):
    assignment: Assignment

    def decide(self, current):
        current.assignment.require_exact(current, self.assignment)
        return current.state.complete(current)


@dataclass(frozen=True, slots=True)
class Todo(TypedTable):
    id: str = field(metadata={"sql": Column(primary_key=True)})
    repo: str
    text: str
    creator: RecordedThreadIncarnation
    created_at: float
    where: TodoWhere
    acceptance: AcceptanceCheck
    state: type[TodoState] = field(metadata={"sql": Column(check=(
        '"state" IN (' + ", ".join(sql_literal(member)
            for member in TodoState.members_with(TodoState)) + ")"))})
    revision: int = field(metadata={"sql": Column(check="revision>0")})
    assignment: TodoAssignment
    last_change: TodoChangeRecord

    def require_revision(self, expected: int) -> None:
        if self.revision != expected:
            raise TodoConflict(self)

    def require_actor(self, actor: RecordedThreadIncarnation):
        if (not self.creator.same_publication_birth(actor)
                and not self.assignment.require_assigned(self).owner.same_publication_birth(actor)):
            raise TodoConflict(self)

    def order_result(self, goals: Goals) -> dict[str, object]:
        assignment = self.assignment.require_assigned(self)
        goal = assignment.goal.require_current(goals.registry)
        return {"todo": FieldCodec.encode(self), "goal": FieldCodec.encode(goal)}

    def require_same_creation(self, proposed: Todo):
        if self != replace(proposed, state=self.state, revision=self.revision,
                           created_at=self.created_at, assignment=self.assignment,
                           last_change=self.last_change):
            raise TodoConflict(self)


@dataclass(frozen=True)
class FleetEntry(DeclaredFamily, affix="FleetEntry"):
    """A problem and the order observation selected by its assignment owner."""

    todo: Todo


class UnassignedFleetEntry(FleetEntry):
    pass


class FormerOrderFleetEntry(FleetEntry):
    """The exact assigned order is no longer current; its reference remains on Todo."""


@dataclass(frozen=True)
class AssignedFleetEntry(UnitObservation, FleetEntry):
    pass


@dataclass(frozen=True)
class TodoRef:
    id: str

    def read(self, root: Path) -> Todo:
        return TodoStore.read_existing(root, self.id)

    def require_current(self, root: Path, goal: GoalRef) -> Todo:
        current = self.read(root)
        if not current.assignment.require_assigned(current).goal.same_order(goal):
            raise TodoConflict(current)
        return current

    def require_completion(self, root: Path, goal: GoalRef, sha: str) -> AcceptedCommit:
        return self.require_current(root, goal).acceptance.check(sha)

    def complete(self, goals: Goals, reference: GoalRef) -> None:
        current = self.require_current(goals.root, reference)
        goal = reference.require_current(goals.registry)
        if not goal.delivery_complete:
            raise TodoError("The assigned Goal has no accepted completion.")
        goal.author.require_order_source(self.id)
        assignment = current.assignment.require_assigned(current)
        TodoStore(goals.root)._apply(current.id, CompleteTodo(current.revision, assignment))


@dataclass(frozen=True)
class CommandedGoalAuthor(GoalAuthor):
    thread: RecordedThreadIncarnation
    issued_at: float
    todo: TodoRef
    revisions: int = 0

    def require_report(self, progress):
        if progress is None:
            raise TodoError("A commanded goal requires a complete structured report.")
        progress.require_report()

    def require_current_order(self, root, goal):
        self.todo.require_current(root, goal)

    def require_order_source(self, problem_id):
        if self.todo.id != problem_id:
            raise TodoError("This goal belongs to another problem.")

    def require_completion(self, progress, *, root, goal):
        self.require_report(progress)
        delivery = progress.require_report().delivery
        delivery.require_completion()
        accepted = self.todo.require_completion(root, goal, delivery.reported_sha)
        return RecordedEventTime(accepted.committed_at)

    def complete_problem(self, goals, reference):
        self.todo.complete(goals, reference)

    def require_self_direction(self):
        raise TodoError("Only the problem's commander can revise this order.")

    def has_report(self, recorded):
        return recorded.since(self.issued_at)

    @property
    def revision_count(self):
        return self.revisions

    def notification_targets(self, registry, root, *, new_order):
        creator = self.todo.read(root).creator
        return tuple(dict.fromkeys(target for target in (creator, self.thread)
                                   if target.current(registry)))

    @property
    def issued_times(self):
        return (self.issued_at,)

    @property
    def instruction(self):
        return (f"Order for problem {self.todo.id} from {self.thread.name}. "
                "Report a complete progress object: stupid, deleted, delivery, next. "
                "Completion requires delivery={\"kind\":\"works_in_app\",\"sha\":\"...\","
                "\"evidence\":\"actual application evidence\"}. Core executes the problem's "
                "declared acceptance check; a mismatch cannot complete the order.")

    def revised(self, author, registry, now):
        if self.thread.resolved(registry) != author:
            raise TodoError("Only this order's original commander can send it back.")
        return replace(self, issued_at=now, revisions=self.revisions + 1)


@dataclass(frozen=True)
class TransferredGoalAuthor(CommandedGoalAuthor):
    previous: GoalRef = field(kw_only=True)

    def notification_targets(self, registry, root, *, new_order):
        current = super().notification_targets(registry, root, new_order=new_order)
        if not new_order:
            return current
        previous = (self.previous.owner,) if self.previous.owner.current(registry) else ()
        return tuple(dict.fromkeys((*current, *previous)))


@dataclass(frozen=True, kw_only=True)
class OrderGoalAction(SetGoalAction):
    author: RecordedThreadIncarnation
    todo: TodoRef
    goal_id: str
    preserve_wait = True

    def require_target(self, goals: Goals, reference: GoalRef):
        target = goals.registry.require(reference.owner.name)
        if target.incarnation != reference.owner:
            raise TodoError("Order target incarnation changed.")
        current = target.goal
        if current is not None and current.id != reference.goal_id and not current.state.terminal:
            raise TodoError("Assignee already has another unresolved goal.")
        if current is not None and current.id == reference.goal_id:
            current.author.require_order_source(self.todo.id)

    def goal_author(self):
        return CommandedGoalAuthor(self.author, time.time(), self.todo)

    def change(self, ctx):
        self.require_target(ctx.goals, GoalRef(ctx.thread.incarnation, self.goal_id))
        snapshot = ctx.goals.registry.snapshot()
        if not self.author.current(snapshot):
            raise TodoError("Order author incarnation changed.")
        current = ctx.thread.goal
        if current is not None and current.id == self.goal_id:
            return current
        goal = Goal(text=self.text.strip(), id=self.goal_id, revision=1, deadline=self.deadline)
        goal = replace(goal, mention_source=bind_goal_mentions(
            goal.text, goal.id, goal.revision, ctx.thread, snapshot, author=self.goal_author()))
        ctx.goals.open_goal_store().create_goal(goal.id)
        return goal

    def before_publish(self, goal, ctx):
        current = ctx.thread.goal
        if current is None or goal.id != current.id:
            super().before_publish(goal, ctx)

    def issue(self, goals: Goals, assignment: Assignment, completed):
        self._apply_locked(GoalActionContext(
            goals, goals.registry.require(assignment.owner.name), RuntimeInvocable,
            completed=completed))


@dataclass(frozen=True, kw_only=True)
class TransferOrderGoalAction(OrderGoalAction):
    previous: GoalRef

    def goal_author(self):
        return TransferredGoalAuthor(self.author, time.time(), self.todo, previous=self.previous)


@dataclass(frozen=True)
class _TodoTable(TypedRow):
    name: str


def _participant(thread: Thread) -> RecordedThreadIncarnation:
    return thread.incarnation.require_recorded()


def _thread(thread: Thread) -> RecordedThreadIncarnation:
    thread.role.require_executable()
    return _participant(thread)


def _repo(value: str) -> str:
    _text(value, "Repo ID", 200)
    pieces = value.split("/")
    if len(pieces) != 2 or any(piece in {"", ".", ".."}
            or not all(char.isalnum() or char in "-_." for char in piece) for piece in pieces):
        raise TodoError("Repo ID must have the exact owner/repo form.")
    return value.lower()


class TodoStore:
    """The existing problem row owns assignment CAS, not a separate task registry."""

    filename = "todos.sqlite3"

    def __init__(self, root: Path):
        directory = Path(root).resolve(strict=True)
        if not directory.is_dir():
            raise TodoError("Todo root must already exist.")
        self.path = directory / self.filename
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            tables = _TodoTable.read(db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
            if not tables:
                Todo.create(db)
            elif tables != [_TodoTable(FieldCodec.encode(Todo))]:
                raise TodoError("Todo storage requires the one-shot durable migration.")
            Todo.select(db, where="0")
            db.execute("COMMIT")

    def _connect(self) -> sqlite3.Connection:
        db = CoordinationStore.writer(self.path, isolation_level=None)
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

    @classmethod
    def read_existing(cls, root: Path, todo_id: str) -> Todo:
        _text(todo_id, "Todo ID", 128)
        with closing(CoordinationStore.read_snapshot(Path(root) / cls.filename)) as db:
            return cls._current(db, todo_id)

    def get(self, todo_id: str) -> Todo:
        return self.read_existing(self.path.parent, todo_id)

    def list(self, repo: str | None = None) -> tuple[Todo, ...]:
        with closing(CoordinationStore.read_snapshot(self.path)) as db:
            if repo is None:
                return tuple(Todo.select(db, order_by=("created_at", "id")))
            return tuple(Todo.read(db.execute(
                f"SELECT * FROM {Todo.declared_name} WHERE repo=? ORDER BY rowid", (_repo(repo),))))

    def fleet(self, registry: Registration, agents: AgentActivity) -> tuple[FleetEntry, ...]:
        snapshot = registry.snapshot()
        activities = agents.all_activity(snapshot.threads.values(), snapshot=snapshot)
        now = time.time()
        return tuple(problem.assignment.observe(problem, snapshot, activities, now)
                     for problem in self.list())

    def create(self, todo_id: str, repo: str, text: str, *, creator: Thread,
               where: TodoWhere, acceptance: AcceptanceCheck, bus: MessageBus) -> Todo:
        _text(todo_id, "Todo ID", 128)
        _text(text, "Todo text", 8000)
        where.require_source(bus)
        proposed = Todo(id=todo_id, repo=_repo(repo), text=text, creator=_participant(creator),
                        created_at=time.time(), where=where, acceptance=acceptance,
                        state=OpenTodoState, revision=1, assignment=UnassignedTodoAssignment(),
                        last_change=OriginalTodoChangeRecord())
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
            changes = {item.name: getattr(result, item.name) for item in fields(Todo)
                       if getattr(result, item.name) != getattr(current, item.name)}
            Todo.update(db, where="id=? AND revision=?", parameters=(todo_id, current.revision),
                        **changes)
            return self._current(db, todo_id)

    def assign(self, todo_id: str, *, expected_revision: int, owner: Thread, parent: Thread,
               generation: str, deadline: GoalDeadline, goals: Goals) -> Todo:
        proposed = Assignment(GoalRef(_thread(owner), generation), _participant(parent))
        with goals.writing() as completed:
            current = self.get(todo_id)
            order = OrderGoalAction(text=current.text, deadline=deadline, author=proposed.parent,
                                    todo=TodoRef(todo_id), goal_id=proposed.generation)
            order.require_target(goals, proposed.goal)
            result = self._apply(todo_id, AssignTodo(expected_revision, proposed))
            order.issue(goals, proposed, completed)
            return result

    def transfer(self, todo_id: str, *, expected_revision: int, previous: Assignment,
                 owner: Thread, parent: Thread, generation: str, deadline: GoalDeadline,
                 goals: Goals) -> Todo:
        proposed = Assignment(GoalRef(_thread(owner), generation), _participant(parent))
        with goals.writing() as completed:
            current = self.get(todo_id)
            order = TransferOrderGoalAction(
                text=current.text, deadline=deadline, author=proposed.parent,
                todo=TodoRef(todo_id), goal_id=proposed.generation, previous=previous.goal)
            order.require_target(goals, proposed.goal)
            result = self._apply(todo_id, TransferTodoChange(expected_revision, previous, proposed))
            ClearGoalAction.end_current(previous.goal, goals=goals, completed=completed)
            order.issue(goals, proposed, completed)
            return result

    def complete(self, todo_id: str, *, expected_revision: int, actor: Thread,
                 progress: GoalProgress, goals: Goals, actor_kind: type) -> Todo:
        current = self.get(todo_id)
        assignment = current.assignment.require_assigned(current)
        if not assignment.owner.same_publication_birth(_participant(actor)):
            raise TodoConflict(current)
        command = CompleteTodo(expected_revision, assignment)
        if current.last_change.matches(command):
            return current
        current.require_revision(expected_revision)
        CompletedGoalAction(
            progress=progress, expect=GoalPrecondition(goal_id=assignment.goal.goal_id)
        ).apply(goals, assignment.owner.name, actor=actor_kind)
        return self.get(todo_id)

    def release(self, todo_id: str, *, expected_revision: int, previous: Assignment,
                goals: Goals) -> Todo:
        with goals.writing() as completed:
            result = self._apply(todo_id, ReleaseTodoChange(expected_revision, previous))
            ClearGoalAction.end_current(previous.goal, goals=goals, completed=completed)
            return result

    def set_state(self, todo_id: str, *, expected_revision: int, state: type[TodoState],
                  actor: Thread, goals: Goals) -> Todo:
        with goals.writing():
            return self._apply(todo_id, ChangeTodoState(expected_revision, state, _participant(actor)))
