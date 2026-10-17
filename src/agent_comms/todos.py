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
from .message_reference import MessageReference
from .goal_actions import (
    ClearGoalAction, CompletedGoalAction, GoalActionContext, GoalPrecondition,
    RuntimeInvocable, SetGoalAction,
)
from .goals import Goal, GoalAuthor, GoalProgress
from .goal_attempt_identity import GoalRef
from .recorded_time import EventTime, RecordedEventTime
from .registration import Registration
from .registry_document import RegistrySnapshot
from .thread_identity import RecordedThreadIncarnation
from .typed_table import Column, TypedRow, TypedTable, sql_literal

if TYPE_CHECKING:
    from .threads import Thread
    from .goal_management import Goals
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


@dataclass(frozen=True)
class SourceTodoWhere:
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


class Assignment(DeclaredFamily, affix="TodoAssignment"):
    def observe(self, problem: Todo, snapshot: RegistrySnapshot,
                activities: Mapping[str, ObservedActivity]) -> TodoObservation:
        return TodoObservation(problem, None, None)

    @property
    def description(self) -> str:
        return "unassigned"

    def require_assigned(self, current: Todo) -> AssignedTodoAssignment:
        raise TodoConflict(current)

    def assign(self, current: Todo, proposed: AssignedTodoAssignment) -> Todo:
        raise TodoConflict(current)

    def require_exact(self, current: Todo, expected: Assignment):
        if self.require_assigned(current) != expected:
            raise TodoConflict(current)

    def require_open(self, current: Todo, snapshot: RegistrySnapshot) -> None:
        pass

    def require_unassigned(self) -> None:
        raise TodoError("Use checked completion of the assigned Goal.")


@dataclass(frozen=True)
class UnassignedTodoAssignment(Assignment):
    def require_unassigned(self):
        pass

    def assign(self, current, proposed):
        return replace(current, assignment=proposed)


@dataclass(frozen=True)
class AssignedTodoAssignment(Assignment):
    goal: GoalRef
    parent: RecordedThreadIncarnation

    def __post_init__(self):
        self.parent.require_recorded()

    def require_successor(self, proposed: AssignedTodoAssignment, current: Todo):
        if (self.goal.goal_id == proposed.goal.goal_id
                or self.goal.owner.same_publication_birth(proposed.goal.owner)):
            raise TodoConflict(current)

    def observe(self, problem, snapshot, activities):
        owner = self.goal.owner
        activity = activities[snapshot.require(owner.name).name] if owner.current(snapshot) else None
        return TodoObservation(problem, self.goal.current(snapshot), activity)

    def require_open(self, current, snapshot):
        goal = self.goal.current(snapshot)
        if goal is not None and goal.delivery_complete:
            raise TodoConflict(current)

    @property
    def description(self):
        return self.goal.owner.name

    def require_assigned(self, current):
        return self


class TodoState(DeclaredFamily, affix="TodoState"):
    @classmethod
    def require_assignment(cls, assignment: Assignment) -> None:
        pass

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
        return replace(current, assignment=command.proposed)


class BlockedTodoState(TodoState):
    pass


class DoneTodoState(TodoState):
    @classmethod
    def change(cls, current, command):
        raise TodoConflict(current)

    @classmethod
    def require_assignment(cls, assignment):
        assignment.require_unassigned()


@dataclass(frozen=True)
class TodoOperation(Command, DeclaredFamily, affix="TodoOperation"):
    expected_revision: int

    def __post_init__(self):
        if self.expected_revision <= 0:
            raise TodoError("Todo operation requires a positive exact revision.")

    def apply(self, current: Todo) -> Todo:
        if current.last_change == self:
            return current
        current.require_revision(self.expected_revision)
        result = self.decide(current)
        if result == current:
            return current
        return replace(result, revision=current.revision + 1,
                       last_change=self)

    @abstractmethod
    def decide(self, current: Todo) -> Todo: ...


@dataclass(frozen=True)
class AssignTodo(TodoOperation):
    proposed: AssignedTodoAssignment

    def decide(self, current):
        current.require_actor(self.proposed.parent)
        return current.state.assign(current, self.proposed)


@dataclass(frozen=True)
class TransferTodoChange(TodoOperation):
    previous: AssignedTodoAssignment
    proposed: AssignedTodoAssignment

    def decide(self, current):
        current.require_actor(self.proposed.parent)
        self.previous.require_successor(self.proposed, current)
        return current.state.transfer(current, self)


@dataclass(frozen=True)
class ReleaseTodoChange(TodoOperation):
    previous: AssignedTodoAssignment

    def decide(self, current):
        return current.state.release(current, self)


@dataclass(frozen=True)
class ChangeTodoState(TodoOperation):
    state: type[TodoState]
    actor: RecordedThreadIncarnation

    def decide(self, current):
        return current.state.change(current, self)


@dataclass(frozen=True, slots=True)
class Todo(TypedTable):
    id: str = field(metadata={"sql": Column(primary_key=True)})
    repo: str
    text: str
    creator: RecordedThreadIncarnation
    created_at: float
    where: SourceTodoWhere | MessageReference
    acceptance: AcceptanceCheck
    state: type[TodoState] = field(metadata={"sql": Column(check=(
        '"state" IN (' + ", ".join(sql_literal(member)
            for member in TodoState.members_with(TodoState)) + ")"))})
    revision: int = field(metadata={"sql": Column(check="revision>0")})
    assignment: Assignment
    last_change: TodoOperation | None

    def __post_init__(self):
        self.state.require_assignment(self.assignment)

    def require_revision(self, expected: int) -> None:
        if self.revision != expected:
            raise TodoConflict(self)

    def require_order(self, reference: GoalRef) -> AssignedTodoAssignment:
        assignment = self.assignment.require_assigned(self)
        if not assignment.goal.same_order(reference):
            raise TodoConflict(self)
        return assignment

    def require_actor(self, actor: RecordedThreadIncarnation):
        if (not self.creator.same_publication_birth(actor)
                and not self.assignment.require_assigned(self).goal.owner.same_publication_birth(actor)):
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
class TodoObservation:
    """A read of canonical problem, exact current order and assignee activity."""

    todo: Todo
    goal: Goal | None
    activity: ObservedActivity | None


@dataclass(frozen=True)
class CommandedGoalAuthor(GoalAuthor):
    thread: RecordedThreadIncarnation
    todo: str

    def require_target(self, goals, target, goal_id):
        if not self.thread.current(goals.registry.snapshot()):
            raise TodoError("Order author incarnation changed.")
        current = target.goal
        if current is not None and current.id == goal_id:
            current.author.require_order_source(self.todo)
        for order in target.open_orders:
            if order.goal_id != goal_id:
                raise TodoError("Assignee already has another unresolved goal.")

    def require_report(self, progress):
        if progress is None:
            raise TodoError("A commanded goal requires a complete structured report.")
        progress.require_report()

    def require_current_order(self, goals, goal):
        current = goals.todos.get(self.todo)
        current.require_order(goal)
        return current

    def require_order_source(self, problem_id):
        if self.todo != problem_id:
            raise TodoError("This goal belongs to another problem.")

    def require_completion(self, progress, *, goals, goal):
        self.require_report(progress)
        delivery = progress.require_report().delivery
        delivery.require_completion()
        accepted = self.require_current_order(goals, goal).acceptance.check(delivery.reported_sha)
        return RecordedEventTime(accepted.committed_at)

    def require_self_direction(self):
        raise TodoError("Only the problem's commander can revise this order.")

    def notification_targets(self, goals, *, new_order):
        creator = goals.todos.get(self.todo).creator
        snapshot = goals.registry.snapshot()
        return tuple(dict.fromkeys(target for target in (creator, self.thread)
                                   if target.current(snapshot)))

    @property
    def instruction(self):
        return (f"Order for problem {self.todo} from {self.thread.name}. "
                "Report a complete progress object: stupid, deleted, delivery, next. "
                "Completion requires delivery={\"kind\":\"works_in_app\",\"sha\":\"...\","
                "\"evidence\":\"actual application evidence\"}. Core executes the problem's "
                "declared acceptance check; a mismatch cannot complete the order.")

    def require_commander(self, actor, snapshot):
        if self.thread.resolved(snapshot) != actor:
            raise TodoError("Only this order's original commander can judge it or send it back.")

    def release(self, goals, reference):
        current = self.require_current_order(goals, reference)
        goals.todos.release(current.id, expected_revision=current.revision,
                            previous=current.assignment.require_assigned(current), goals=goals)


@dataclass(frozen=True)
class TransferredGoalAuthor(CommandedGoalAuthor):
    previous: GoalRef = field(kw_only=True)

    def notification_targets(self, goals, *, new_order):
        current = super().notification_targets(goals, new_order=new_order)
        if not new_order:
            return current
        previous = (self.previous.owner,) if self.previous.owner.current(goals.registry.snapshot()) else ()
        return tuple(dict.fromkeys((*current, *previous)))


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

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(CoordinationStore.writer(self.path, isolation_level=None)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA synchronous=FULL")
            try:
                db.execute("BEGIN IMMEDIATE")
                tables = _TodoTable.read(db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
                if not tables:
                    Todo.create(db)
                elif tables != [_TodoTable(FieldCodec.encode(Todo))]:
                    raise TodoError("Todo storage requires the one-shot durable migration.")
                Todo.select(db, where="0")
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
        try:
            self.path.lstat()
        except FileNotFoundError as error:
            raise TodoError(f"Unknown todo: {todo_id}.") from error
        with closing(CoordinationStore.read_snapshot(self.path)) as db:
            return self._current(db, todo_id)

    def list(self, repo: str | None = None) -> tuple[Todo, ...]:
        if repo is not None:
            repo = _repo(repo)
        try:
            self.path.lstat()
        except FileNotFoundError:
            return ()
        with closing(CoordinationStore.read_snapshot(self.path)) as db:
            if repo is None:
                return tuple(Todo.select(db, order_by=("created_at", "id")))
            return tuple(Todo.read(db.execute(
                f"SELECT * FROM {Todo.declared_name} WHERE repo=? ORDER BY rowid", (repo,))))

    def fleet(self, registry: Registration, agents: AgentActivity) -> tuple[TodoObservation, ...]:
        problems = self.list()
        if not problems:
            return ()
        snapshot = registry.snapshot()
        activities = agents.all_activity(snapshot.threads.values(), snapshot=snapshot)
        return tuple(problem.assignment.observe(problem, snapshot, activities)
                     for problem in problems)

    def create(self, todo_id: str, repo: str, text: str, *, creator: Thread,
               where: SourceTodoWhere | MessageReference,
               acceptance: AcceptanceCheck, bus: MessageBus) -> Todo:
        _text(todo_id, "Todo ID", 128)
        _text(text, "Todo text", 8000)
        if isinstance(where, MessageReference):
            bus.log.messages_for_references((where,))
        else:
            where.require_source(bus)
        proposed = Todo(id=todo_id, repo=_repo(repo), text=text, creator=_participant(creator),
                        created_at=time.time(), where=where, acceptance=acceptance,
                        state=OpenTodoState, revision=1, assignment=UnassignedTodoAssignment(),
                        last_change=None)
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
               generation: str, deadline: EventTime, goals: Goals) -> Todo:
        proposed = AssignedTodoAssignment(GoalRef(_thread(owner), generation), _participant(parent))
        with goals.writing() as completed:
            current = self.get(todo_id)
            order = SetGoalAction(text=current.text, deadline=deadline,
                author=CommandedGoalAuthor(proposed.parent, todo_id), goal_id=proposed.goal.goal_id)
            order.require_target(goals, proposed.goal)
            result = self._apply(todo_id, AssignTodo(expected_revision, proposed))
            order._apply_locked(GoalActionContext(
                goals, goals.registry.require(proposed.goal.owner.name), RuntimeInvocable,
                completed=completed))
            return result

    def transfer(self, todo_id: str, *, expected_revision: int, previous: AssignedTodoAssignment,
                 owner: Thread, parent: Thread, generation: str, deadline: EventTime,
                 goals: Goals) -> Todo:
        proposed = AssignedTodoAssignment(GoalRef(_thread(owner), generation), _participant(parent))
        with goals.writing() as completed:
            current = self.get(todo_id)
            current.assignment.require_open(current, goals.registry.snapshot())
            order = SetGoalAction(text=current.text, deadline=deadline,
                author=TransferredGoalAuthor(proposed.parent, todo_id, previous=previous.goal),
                goal_id=proposed.goal.goal_id)
            order.require_target(goals, proposed.goal)
            result = self._apply(todo_id, TransferTodoChange(expected_revision, previous, proposed))
            ClearGoalAction.end_current(previous.goal, goals=goals, completed=completed)
            order._apply_locked(GoalActionContext(
                goals, goals.registry.require(proposed.goal.owner.name), RuntimeInvocable,
                completed=completed))
            return result

    def complete(self, todo_id: str, *, expected_revision: int, actor: Thread,
                 progress: GoalProgress, goals: Goals, actor_kind: type) -> Todo:
        current = self.get(todo_id)
        assignment = current.assignment.require_assigned(current)
        if not assignment.goal.owner.same_publication_birth(_participant(actor)):
            raise TodoConflict(current)
        current.require_revision(expected_revision)
        CompletedGoalAction.ensure(
            assignment.goal.require_current(goals.registry), assignment.goal,
            goals=goals, progress=progress, actor=actor_kind)
        return current

    def release(self, todo_id: str, *, expected_revision: int, previous: AssignedTodoAssignment,
                goals: Goals) -> Todo:
        with goals.writing() as completed:
            current = self.get(todo_id)
            current.assignment.require_open(current, goals.registry.snapshot())
            result = self._apply(todo_id, ReleaseTodoChange(expected_revision, previous))
            ClearGoalAction.end_current(previous.goal, goals=goals, completed=completed)
            return result

    def set_state(self, todo_id: str, *, expected_revision: int, state: type[TodoState],
                  actor: Thread, goals: Goals) -> Todo:
        with goals.writing():
            current = self.get(todo_id)
            current.assignment.require_open(current, goals.registry.snapshot())
            return self._apply(todo_id, ChangeTodoState(expected_revision, state, _participant(actor)))
