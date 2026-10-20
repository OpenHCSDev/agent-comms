"""Exercise current order commands against real native owners on a selected bus copy."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shlex
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from pydantic import PrivateAttr

from ..change_report import FileLine
from ..check_root import CheckRoot
from ..child_process import BoundedRun, ChildResult
from ..commander.config import CommanderConfig
from ..commander.order import Commander, OrderFile
from ..commander.request import RequestAddVerb
from ..field_codec import FieldCodec
from ..goals import CountCheckResult, OrderCheck, ProblemGoalOrder
from ..recorded_time import RecordedEventTime
from ..todos import (
    ApplicationOrderRun,
    MergedProgress,
    Moving,
    NoCommitProgress,
    PendingProblemOrder,
    Todo,
)
from ..tool_caller import ThreadCaller, ToolCaller
from ..tool_result import ToolResult
from ..tools import (
    CommsChangeProblemTool,
    CommsFireTool,
    CommsOrderTool,
    CommsProblemCloseTool,
    CommsTodoCreateTool,
    CommsTodoListTool,
    ProblemOrdered,
    TodoChanged,
    TodoList,
    ToolRequest,
)
from . import ParityComparison, UnverifiedParity


@dataclass(frozen=True)
class LifecycleObservation(ParityComparison):
    owner: str
    invocations: int
    checks: tuple[tuple[str, bool], ...]
    output: str

    @property
    def passed(self) -> bool:
        return bool(self.invocations and self.checks) and all(value for _, value in self.checks)

    def describe(self) -> str:
        return (
            f"owner={self.owner}; calls={self.invocations}; "
            f"checks={dict(self.checks)}; output={self.output!r}"
        )


type LifecycleRequest = ToolRequest[ToolCaller] | ToolRequest[ThreadCaller]


@dataclass(frozen=True)
class NativeInvocation:
    request: LifecycleRequest
    result: ToolResult


class MeasuredCommander(Commander):
    """Keep actual native returns at the existing command boundary, not frame locals."""

    _invocations: list[NativeInvocation] = PrivateAttr(default_factory=list)

    @property
    def invocations(self) -> tuple[NativeInvocation, ...]:
        return tuple(self._invocations)

    def call(self, tool: LifecycleRequest) -> ToolResult:
        result: ToolResult = super().call(tool)
        self._invocations.append(NativeInvocation(tool, result))
        return result


@dataclass(frozen=True)
class LifecycleRun:
    commander: MeasuredCommander
    worker: str
    decisions: Path
    document: Path
    artifact: Path

    @classmethod
    @contextlib.contextmanager
    def open(cls, *, extra_threads: tuple[str, ...] = ()) -> Iterator[LifecycleRun]:
        config = MeasuredCommander.read(CommanderConfig.path())
        source = config.comms
        actor = ToolCaller.from_actor(os.environ.get("AGENT_COMMS_THREAD")).acting_name(
            config.commander
        )
        caller = source.registry.require(actor)
        if actor == config.commander:
            held = source.goals.todos.held_for_commander(config.incarnation)
            if not held:
                raise ValueError("Lifecycle comparison requires an existing native order holder")
            worker = source.registry.require(held[-1].held().owner)
        else:
            worker = caller
        directory = caller.storage_roots.scratch / "commander-parity" / uuid4().hex
        threads = tuple(dict.fromkeys((config.commander, worker.name, *extra_threads)))
        with CheckRoot(source, threads, directory).open() as copied:
            root = copied.root
            commander = MeasuredCommander(
                commander=config.commander,
                root=root,
                cli=config.cli,
                state=root / "commander-state",
                board=root / "board.md",
                narrative=root / "narrative.md",
                repos=config.repos,
                worktrees=root / "projects",
                scratch=root / "scratch",
                disk=root,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                RequestAddVerb(
                    text=(
                        "Compare current order commands with their real native owners in this "
                        "disposable copied bus, preserving the source bus and its sessions."
                    )
                ).run(commander)
            decisions = root / "decisions.json"
            decisions.write_text(json.dumps([worker.name]))
            command = shlex.join(
                (
                    sys.executable,
                    "-c",
                    "import json;from pathlib import Path;"
                    f"print(len(json.loads(Path({str(decisions)!r}).read_text())))",
                )
            )
            order = PendingProblemOrder(
                owner=FileLine(str(Path(__file__).resolve()), 1),
                check=OrderCheck(
                    str(Path(__file__).resolve().parents[3]), command, CountCheckResult(0)
                ),
                edits=(
                    "Remove the copied workflow decision and verify its native order completes.",
                ),
                banned=("Do not change the source bus or its saved sessions.",),
                report="Report the actual copied goal and native order state.",
                run=ApplicationOrderRun(
                    "Read the copied native goal and confirm its recorded source location "
                    "and assigned worker."
                ),
            )
            document = root / "order.json"
            document.write_text(
                json.dumps(
                    FieldCodec.encode(
                        OrderFile(
                            order,
                            "The copied workflow decision prevents the native completion check "
                            "from reaching zero.",
                            "This is stupid because an unfinished copied decision must not count "
                            "as delivered work.",
                        )
                    )
                )
            )
            yield cls(commander, worker.name, decisions, document, root / "application.json")

    @staticmethod
    def python_environment() -> dict[str, str]:
        return {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}

    @classmethod
    def invoke(cls, program: str, *arguments: str) -> ChildResult:
        return cls.invoke_command(
            (sys.executable, "-c", program, *arguments),
            cwd=Path.cwd(),
            env=cls.python_environment(),
        )

    @staticmethod
    def invoke_command(
        command: tuple[str, ...],
        *,
        cwd: str | Path,
        env: dict[str, str],
    ) -> ChildResult:
        return asyncio.run(BoundedRun.run(command, timeout=120, cwd=cwd, env=env))

    @property
    def due(self) -> str:
        return RecordedEventTime(time.time() + 3600).clock()

    @property
    def deadline(self) -> RecordedEventTime:
        return RecordedEventTime(time.time()).at_clock(self.due)

    def create(self) -> Todo:
        self.commander.problem(
            str(self.document), self.worker, self.due, f"{Path(__file__).resolve()}:1"
        )
        rows = tuple(self.commander.comms.goals.todos.for_commander(self.commander.incarnation))
        if len(rows) != 1:
            raise ValueError(f"A fresh copied workflow must create one problem, got {len(rows)}")
        return rows[0]

    def complete(self, todo: Todo) -> Todo:
        goal = self.commander.comms.registry.require(self.worker).goal
        self.artifact.write_text(json.dumps(FieldCodec.encode(goal)))
        self.decisions.write_text("[]")
        self.commander.done(todo.id, "op", seen=str(self.artifact))
        return self.commander.comms.goals.todos.get(todo.id)


class LifecycleCase(ABC):
    native_owner: ClassVar[type[LifecycleRequest]]
    additional_owners: ClassVar[tuple[type[LifecycleRequest], ...]] = ()

    @classmethod
    @abstractmethod
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]: ...

    @classmethod
    @abstractmethod
    def checks(
        cls, run: LifecycleRun, rows: tuple[Todo, ...], native: tuple[ToolResult, ...], output: str
    ) -> tuple[tuple[str, bool], ...]: ...

    @classmethod
    def read(cls) -> ParityComparison:
        owners = (cls.native_owner, *cls.additional_owners)
        with LifecycleRun.open() as run:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rows = cls.exercise(run)
            native = tuple(
                invocation.result
                for invocation in run.commander.invocations
                if isinstance(invocation.request, owners)
            )
            text = output.getvalue()
            return LifecycleObservation(
                ", ".join(f"{owner.__module__}.{owner.__qualname__}" for owner in owners),
                len(native),
                cls.checks(run, rows, native, text),
                text,
            )


class CreateProblemCase(LifecycleCase):
    native_owner: ClassVar[type[ToolRequest[ThreadCaller]]] = CommsTodoCreateTool

    @classmethod
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]:
        return (run.create(),)

    @classmethod
    def checks(
        cls, run: LifecycleRun, rows: tuple[Todo, ...], native: tuple[ToolResult, ...], output: str
    ) -> tuple[tuple[str, bool], ...]:
        if not native or not isinstance(native[-1], ProblemOrdered):
            return (("native order result returned", False),)
        result, saved = native[-1], rows[-1]
        goal = run.commander.comms.registry.require(run.worker).goal
        return (
            ("printed native identity", output.rstrip().endswith(cls.printed_identity(result))),
            (
                "native source retained",
                saved.order.checked().owner == result.todo.order.checked().owner,
            ),
            ("native assignment retained", saved.assignment == result.todo.assignment),
            ("native goal published", goal.require().id == result.goal.id),
        )

    @classmethod
    def printed_identity(cls, result: ProblemOrdered) -> str:
        return result.todo.id


class AssignOrderCase(CreateProblemCase):
    native_owner = CommsOrderTool

    @classmethod
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]:
        before = run.create()
        run.commander.fire(
            run.worker, "The copied workflow is released before its next native assignment."
        )
        document = replace(OrderFile.read(run.document.read_text()), problems=(before.id,))
        run.commander.send(run.worker, run.due, document, clean=True)
        return before, run.commander.comms.goals.todos.get(before.id)

    @classmethod
    def checks(
        cls, run: LifecycleRun, rows: tuple[Todo, ...], native: tuple[ToolResult, ...], output: str
    ) -> tuple[tuple[str, bool], ...]:
        inherited = super().checks(run, rows, native, output)
        return (
            *inherited,
            ("same problem reassigned", rows[0].id == rows[-1].id),
            ("prior assignment retained", bool(rows[-1].released)),
        )

    @classmethod
    def printed_identity(cls, result: ProblemOrdered) -> str:
        return result.todo.order.checked().id


class InstalledEvidenceCase(LifecycleCase):
    native_owner = CommsProblemCloseTool

    @classmethod
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]:
        created = run.create()
        delivered = run.complete(created)
        run.commander.close(
            created.id,
            "Read the copied native goal and confirm its recorded source location "
            "and assigned worker.",
            str(run.artifact),
        )
        return delivered, run.commander.comms.goals.todos.get(created.id)

    @classmethod
    def checks(
        cls, run: LifecycleRun, rows: tuple[Todo, ...], native: tuple[ToolResult, ...], output: str
    ) -> tuple[tuple[str, bool], ...]:
        return (
            ("delivery alone unaccepted", not rows[0].order.accepted),
            ("installed evidence accepted", rows[-1].order.accepted),
            ("closed in native store", not rows[-1].state.open),
            ("actual artifact exists", run.artifact.is_file()),
            ("current close printed", f"{rows[-1].id} closed" in output),
        )


class BuildBoundProofCase(InstalledEvidenceCase):
    @classmethod
    def read(cls) -> ParityComparison:
        closing = super().read()
        return UnverifiedParity(
            "Missing native owner: a build-bound application-run record linking the "
            "runtime and Core/Toad revisions to closing evidence. The actual close "
            "owner currently accepts a bare artifact path. " + closing.describe()
        )


class StrikeLimitCase(LifecycleCase):
    native_owner = CommsChangeProblemTool
    additional_owners = (CommsFireTool,)

    @classmethod
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]:
        created = run.create()
        for number in range(ProblemGoalOrder.StrikeLimit):
            run.commander.strike(
                run.worker,
                created.id,
                "The copied decision remains unfinished at its recorded review "
                f"number {number + 1}.",
            )
        run.commander.fire(
            run.worker, "The copied holder exhausted its declared live strikes on this problem."
        )
        return created, run.commander.comms.goals.todos.get(created.id)

    @classmethod
    def checks(
        cls, run: LifecycleRun, rows: tuple[Todo, ...], native: tuple[ToolResult, ...], output: str
    ) -> tuple[tuple[str, bool], ...]:
        saved = rows[-1]
        return (
            ("declared limit recorded", saved.strike_count == ProblemGoalOrder.StrikeLimit),
            ("one native fire", sum(isinstance(result, TodoList) for result in native) == 1),
            (
                "assignment released",
                saved.id
                not in {
                    todo.id
                    for todo in run.commander.comms.goals.todos.held_for_commander(
                        run.commander.incarnation
                    )
                },
            ),
            ("current strike count printed", f"{ProblemGoalOrder.StrikeLimit} strike(s)" in output),
        )


class OrderProgressMutationCase(LifecycleCase):
    native_owner = CommsChangeProblemTool

    @classmethod
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]:
        before = run.create()
        moved = time.strftime("%H:%M", time.localtime(time.time() + 7200))
        run.commander.due(
            before.id,
            moved,
            "The copied workflow needs time to record its actual native application evidence.",
        )
        return before, run.commander.comms.goals.todos.get(before.id)

    @classmethod
    def checks(
        cls, run: LifecycleRun, rows: tuple[Todo, ...], native: tuple[ToolResult, ...], output: str
    ) -> tuple[tuple[str, bool], ...]:
        before, after = rows
        messages = run.commander.comms.bus.inbox(run.worker)
        unit = run.commander.comms.registry.require(run.worker)
        goal = unit.goal.require()
        instructions = goal.order.instructions(
            goal.order.todos(run.commander.comms.goals, unit.incarnation, goal.id)
        )
        native_deadlines = tuple(
            result.todo.deadline for result in native if isinstance(result, TodoChanged)
        )
        return (
            (
                "native deadline retained",
                bool(native_deadlines) and native_deadlines[-1] == after.deadline,
            ),
            ("deadline actually changed", before.deadline != after.deadline),
            ("assignment unchanged", before.assignment == after.assignment),
            ("unit got the actual update", bool(messages)),
            (
                "unit messages hide clocks",
                all(
                    clock.clock() not in message.body
                    for clock in (before.deadline, after.deadline)
                    for message in messages
                ),
            ),
            (
                "unit goal hides deadline",
                "Deadline:" not in instructions and after.deadline.clock() not in instructions,
            ),
            ("commander still sees deadline", f"due {after.deadline.clock()}" in output),
        )


@dataclass(frozen=True)
class ProgressComparison(ParityComparison):
    current: dict[str, str]
    native: dict[str, str]
    first_seconds: int
    later_seconds: int
    merged_no_strikes: tuple[bool, ...]
    workflow: ParityComparison

    @property
    def passed(self) -> bool:
        return all(
            (
                self.workflow.passed,
                bool(self.native),
                self.current == self.native,
                self.first_seconds == 600,
                self.later_seconds == 900,
                bool(self.merged_no_strikes),
                all(self.merged_no_strikes),
            )
        )

    def describe(self) -> str:
        return (
            f"{self.workflow.describe()}; live_progress={len(self.native)}; "
            f"read_equal={self.current == self.native}; "
            f"intervals={self.first_seconds}/{self.later_seconds}; "
            f"live_merged_no_strikes={self.merged_no_strikes}"
        )


def verify_order_progress() -> ParityComparison:
    context = MeasuredCommander.read(CommanderConfig.path())
    current = {key: value.line for key, value in context.progress().items()}
    results = tuple(
        invocation.result
        for invocation in context.invocations
        if isinstance(invocation.request, CommsTodoListTool)
    )
    if len(results) != 1 or not isinstance(results[0], TodoList):
        return UnverifiedParity("Current progress did not return one native TodoList measurement")
    result = results[0]
    observed = {key: value.line for key, value in result.progress.items()}
    todos = {todo.id: todo for todo in result.todos}
    merged = tuple(
        not progress.stalled(todos[pid].order.checked().at.recorded_at("An order"), time.time())
        for pid, progress in result.progress.items()
        if isinstance(progress, MergedProgress)
    )
    return ProgressComparison(
        current,
        observed,
        NoCommitProgress.seconds,
        Moving.seconds,
        merged,
        OrderProgressMutationCase.read(),
    )
