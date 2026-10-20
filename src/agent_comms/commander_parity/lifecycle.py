"""Exercise current order commands against real native owners on a selected bus copy."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import FrameType, FunctionType
from typing import ClassVar
from uuid import uuid4

from ..change_report import FileLine
from ..check_root import CheckRoot
from ..commander.config import CommanderConfig
from ..commander.order import Commander, OrderFile, hhmm
from ..commander.request import RequestAddVerb
from ..comms import Comms
from ..field_codec import FieldCodec
from ..goals import CountCheckResult, OrderCheck, ProblemGoalOrder
from ..tool_caller import ToolCaller
from ..tool_result import ToolResult
from ..tools import (
    CommsChangeProblemTool, CommsFireTool, CommsOrderTool,
    CommsProblemCloseTool, CommsTodoCreateTool, ProblemOrdered, ThreadToolRequest, TodoChanged, TodoList,
)
from ..todos import (
    ApplicationOrderRun, MergedProgress, Moving, NoCommitProgress,
    OrderProgress, PendingProblemOrder, Todo,
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
        return f"owner={self.owner}; calls={self.invocations}; checks={dict(self.checks)}; output={self.output!r}"


@dataclass(frozen=True)
class LifecycleRun:
    commander: Commander
    worker: str
    decisions: Path
    document: Path
    artifact: Path

    @classmethod
    @contextlib.contextmanager
    def open(cls) -> Iterator[LifecycleRun]:
        config = CommanderConfig.read(CommanderConfig.path())
        source = Comms(config.root)
        actor = ToolCaller.from_actor(os.environ.get("AGENT_COMMS_THREAD")).acting_name(config.commander)
        worker = source.registry.require(actor)
        if worker.name == config.commander:
            raise ValueError("Lifecycle comparison requires an existing unit caller, not its commander")
        directory = worker.storage_roots.scratch / "commander-parity" / uuid4().hex
        with CheckRoot(source, (config.commander, worker.name), directory).open() as copied:
            root = copied.root
            commander = Commander(
                commander=config.commander, root=root, cli=config.cli,
                state=root / "commander-state", board=root / "board.md",
                narrative=root / "narrative.md", repos=config.repos,
                worktrees=root / "projects", scratch=root / "scratch", disk=root,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                RequestAddVerb(text=(
                    "Compare current order commands with their real native owners in this "
                    "disposable copied bus, preserving the source bus and its sessions.")).run(commander)
            decisions = root / "decisions.json"
            decisions.write_text(json.dumps([worker.name]))
            command = shlex.join((sys.executable, "-c",
                "import json;from pathlib import Path;"
                f"print(len(json.loads(Path({str(decisions)!r}).read_text())))"))
            order = PendingProblemOrder(
                owner=FileLine(str(Path(__file__).resolve()), 1),
                check=OrderCheck(str(Path(__file__).resolve().parents[3]), command, CountCheckResult(0)),
                edits=("Remove the copied workflow decision and verify its native order completes.",),
                banned=("Do not change the source bus or its saved sessions.",),
                report="Report the actual copied goal and native order state.",
                run=ApplicationOrderRun(
                    "Read the copied native goal and confirm its recorded source location and assigned worker."),
            )
            document = root / "order.json"
            document.write_text(json.dumps(FieldCodec.encode(OrderFile(
                order, "The copied workflow decision prevents the native completion check from reaching zero.",
                "This is stupid because an unfinished copied decision must not count as delivered work."))))
            yield cls(commander, worker.name, decisions, document, root / "application.json")

    @property
    def due(self) -> str:
        return time.strftime("%H:%M", time.localtime(time.time() + 3600))

    def create(self) -> Todo:
        self.commander.problem(str(self.document), self.worker, self.due,
                               f"{Path(__file__).resolve()}:1")
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
    native_owner: ClassVar[type[ThreadToolRequest]]
    additional_owners: ClassVar[tuple[type[ThreadToolRequest], ...]] = ()

    @classmethod
    @abstractmethod
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]: ...

    @classmethod
    @abstractmethod
    def checks(cls, run: LifecycleRun, rows: tuple[Todo, ...],
               native: tuple[ToolResult, ...], output: str) -> tuple[tuple[str, bool], ...]: ...

    @classmethod
    def read(cls) -> ParityComparison:
        owners = (cls.native_owner, *cls.additional_owners)
        codes = set()
        for owner in owners:
            function = owner.apply
            if not isinstance(function, FunctionType):
                raise TypeError("A native tool must declare its executable apply method")
            codes.add(function.__code__)
        native: list[ToolResult] = []

        def observe(frame: FrameType, event: str, value: object) -> None:
            if event == "return" and frame.f_code in codes:
                request = frame.f_locals["self"]
                if type(request) in owners and isinstance(value, ToolResult):
                    native.append(value)

        with LifecycleRun.open() as run:
            previous = sys.getprofile()
            output = io.StringIO()
            sys.setprofile(observe)
            try:
                with contextlib.redirect_stdout(output):
                    rows = cls.exercise(run)
            finally:
                sys.setprofile(previous)
            text = output.getvalue()
            return LifecycleObservation(
                ", ".join(f"{owner.__module__}.{owner.__qualname__}" for owner in owners), len(native),
                cls.checks(run, rows, tuple(native), text), text)


class CreateProblemCase(LifecycleCase):
    native_owner = CommsTodoCreateTool

    @classmethod
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]:
        return (run.create(),)

    @classmethod
    def checks(cls, run: LifecycleRun, rows: tuple[Todo, ...],
               native: tuple[ToolResult, ...], output: str) -> tuple[tuple[str, bool], ...]:
        if len(native) != 1 or not isinstance(native[0], ProblemOrdered):
            return (("one native creation result", False),)
        result, saved = native[0], rows[-1]
        goal = run.commander.comms.registry.require(run.worker).goal
        return (("printed native identity", output.rstrip().endswith(cls.printed_identity(result))),
                ("native source retained", saved.order.checked().owner == result.todo.order.checked().owner),
                ("native assignment retained", saved.assignment == result.todo.assignment),
                ("native goal published", goal.require().id == result.goal.id))

    @classmethod
    def printed_identity(cls, result: ProblemOrdered) -> str:
        return result.todo.id


class AssignOrderCase(CreateProblemCase):
    native_owner = CommsOrderTool

    @classmethod
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]:
        before = run.create()
        run.commander.fire(run.worker, "The copied workflow is released before its next native assignment.")
        document = replace(OrderFile.read(run.document.read_text()), problems=(before.id,))
        run.commander.send(run.worker, run.due, document, clean=True)
        return before, run.commander.comms.goals.todos.get(before.id)

    @classmethod
    def checks(cls, run: LifecycleRun, rows: tuple[Todo, ...],
               native: tuple[ToolResult, ...], output: str) -> tuple[tuple[str, bool], ...]:
        inherited = super().checks(run, rows, native, output)
        return (*inherited, ("same problem reassigned", rows[0].id == rows[-1].id),
                ("prior assignment retained", bool(rows[-1].released)))

    @classmethod
    def printed_identity(cls, result: ProblemOrdered) -> str:
        return result.todo.order.checked().id


class InstalledEvidenceCase(LifecycleCase):
    native_owner = CommsProblemCloseTool

    @classmethod
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]:
        created = run.create()
        delivered = run.complete(created)
        run.commander.close(created.id,
            "Read the copied native goal and confirm its recorded source location and assigned worker.",
            str(run.artifact))
        return delivered, run.commander.comms.goals.todos.get(created.id)

    @classmethod
    def checks(cls, run: LifecycleRun, rows: tuple[Todo, ...],
               native: tuple[ToolResult, ...], output: str) -> tuple[tuple[str, bool], ...]:
        return (("delivery alone unaccepted", not rows[0].order.accepted),
                ("installed evidence accepted", rows[-1].order.accepted),
                ("closed in native store", not rows[-1].state.open),
                ("actual artifact exists", run.artifact.is_file()),
                ("current close printed", f"{rows[-1].id} closed" in output))


class StrikeLimitCase(LifecycleCase):
    native_owner = CommsChangeProblemTool
    additional_owners = (CommsFireTool,)

    @classmethod
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]:
        created = run.create()
        for number in range(ProblemGoalOrder.StrikeLimit):
            run.commander.strike(run.worker, created.id,
                f"The copied decision remains unfinished at its recorded review number {number + 1}.")
        run.commander.fire(run.worker,
            "The copied holder exhausted its declared live strikes on this problem.")
        return created, run.commander.comms.goals.todos.get(created.id)

    @classmethod
    def checks(cls, run: LifecycleRun, rows: tuple[Todo, ...],
               native: tuple[ToolResult, ...], output: str) -> tuple[tuple[str, bool], ...]:
        saved = rows[-1]
        return (("declared limit recorded", saved.strike_count == ProblemGoalOrder.StrikeLimit),
                ("one native fire", sum(isinstance(result, TodoList) for result in native) == 1),
                ("assignment released", saved.holder is None),
                ("current strike count printed", f"{ProblemGoalOrder.StrikeLimit} strike(s)" in output))


class OrderProgressMutationCase(LifecycleCase):
    native_owner = CommsChangeProblemTool

    @classmethod
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]:
        before = run.create()
        moved = time.strftime("%H:%M", time.localtime(time.time() + 7200))
        run.commander.due(before.id, moved,
            "The copied workflow needs time to record its actual native application evidence.")
        return before, run.commander.comms.goals.todos.get(before.id)

    @classmethod
    def checks(cls, run: LifecycleRun, rows: tuple[Todo, ...],
               native: tuple[ToolResult, ...], output: str) -> tuple[tuple[str, bool], ...]:
        before, after = rows
        messages = run.commander.comms.bus.inbox(run.worker)
        unit = run.commander.comms.registry.require(run.worker)
        goal = unit.goal.require()
        instructions = goal.order.instructions(goal.order.todos(
            run.commander.comms.goals, unit.incarnation, goal.id))
        native_deadlines = tuple(result.todo.deadline for result in native if isinstance(result, TodoChanged))
        return (("native deadline retained", bool(native_deadlines) and native_deadlines[-1] == after.deadline),
                ("deadline actually changed", before.deadline != after.deadline),
                ("assignment unchanged", before.assignment == after.assignment),
                ("unit got the actual update", bool(messages)),
                ("unit messages hide clocks", all(hhmm(clock) not in message.body
                    for clock in (before.deadline, after.deadline) for message in messages)),
                ("unit goal hides deadline", "Deadline:" not in instructions
                    and hhmm(after.deadline) not in instructions),
                ("commander still sees deadline", f"due {hhmm(after.deadline)}" in output))


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
        return (self.workflow.passed and bool(self.native) and self.current == self.native
                and self.first_seconds == 600 and self.later_seconds == 900
                and bool(self.merged_no_strikes) and all(self.merged_no_strikes))

    def describe(self) -> str:
        return (f"{self.workflow.describe()}; live_progress={len(self.native)}; "
                f"read_equal={self.current == self.native}; intervals={self.first_seconds}/{self.later_seconds}; "
                f"live_merged_no_strikes={self.merged_no_strikes}")


def verify_order_progress() -> ParityComparison:
    context = Commander.read(CommanderConfig.path())
    observed: dict[str, str] = {}
    merged: list[bool] = []

    def observe(frame: FrameType, event: str, value: object) -> None:
        if event == "return" and frame.f_code is OrderProgress.read.__func__.__code__:
            todo = frame.f_locals["todo"]
            if not isinstance(todo, Todo) or not isinstance(value, OrderProgress):
                raise TypeError("The native progress reader returned an unexpected record")
            observed[todo.id] = value.line
            if isinstance(value, MergedProgress):
                merged.append(not value.stalled(todo.order.checked().at.recorded_at("An order"), time.time()))

    previous = sys.getprofile()
    sys.setprofile(observe)
    try:
        current = {key: value.line for key, value in context.progress().items()}
    finally:
        sys.setprofile(previous)
    return ProgressComparison(current, observed, NoCommitProgress.seconds, Moving.seconds,
                              tuple(merged), OrderProgressMutationCase.read())
