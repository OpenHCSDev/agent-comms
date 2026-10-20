"""Exercise current order commands against real native owners on a selected bus copy."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from pydantic import PrivateAttr

from agent_comms.change_report import FileLine
from agent_comms.check_root import CheckRoot
from agent_comms.child_process import BoundedRun, ChildResult
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.order import Commander, OrderFile
from agent_comms.commander.request import RequestAddVerb
from agent_comms.commander_parity import ParityComparison, UnverifiedParity
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import CountCheckResult, OrderCheck, ProblemGoalOrder
from agent_comms.launch_runtime import ProofBuildMismatchError, RecordedBuildIdentity
from agent_comms.progress_claims import WorkingClaim
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import (
    ApplicationOrderRun,
    DeliveredProblemOrder,
    FileDeliveryEvidence,
    MergedProgress,
    Moving,
    NoCommitProgress,
    OperationDelivery,
    PendingProblemOrder,
    Todo,
)
from agent_comms.tool_caller import ThreadCaller, ToolCaller
from agent_comms.tool_result import ToolResult
from agent_comms.tools import (
    CommsChangeProblemTool,
    CommsFireTool,
    CommsOrderDoneTool,
    CommsOrderTool,
    CommsProblemCloseTool,
    CommsTodoCreateTool,
    CommsTodoListTool,
    ProblemOrdered,
    TodoChanged,
    TodoList,
    ToolRequest,
)


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
                state=root / "commander-state",
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
            (sys.executable, "-c", program, *arguments), env=cls.python_environment()
        )

    @staticmethod
    def invoke_command(command: tuple[str, ...], **options) -> ChildResult:
        """BoundedRun.run owns the process options and their absent defaults."""
        return asyncio.run(BoundedRun.run(command, timeout=120, **options))

    @property
    def due(self) -> str:
        return time.strftime("%H:%M", time.localtime(time.time() + 3600))

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
        run.commander.send(run.worker, RecordedEventTime.from_clock(run.due), document, clean=True)
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
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]:
        delivered, closed = super().exercise(run)
        stale_run = replace(run, artifact=run.artifact.with_name("older-build-proof.json"))
        stale_run.decisions.write_text(json.dumps([stale_run.worker]))
        created = stale_run.create()
        installed = RecordedBuildIdentity.installed()
        older = subprocess.check_output(
            ("git", "rev-parse", f"{installed.core}^"),
            cwd=created.order.checked().check.directory,
            text=True,
        ).strip()
        stale_run.artifact.write_text(
            json.dumps(
                FieldCodec.encode(stale_run.commander.comms.registry.require(stale_run.worker).goal)
            )
        )
        stale_run.decisions.write_text("[]")
        stale_run.commander.call(
            CommsOrderDoneTool(
                problem=created.id,
                order_id=created.order.checked().id,
                delivery=OperationDelivery(
                    evidence=FileDeliveryEvidence(str(stale_run.artifact)),
                    proof_build=replace(installed, core=older),
                ),
                claims=(WorkingClaim(str(stale_run.artifact)),),
            )
        )
        stale = stale_run.commander.comms.goals.todos.get(created.id)
        try:
            stale_run.commander.close(
                stale.id,
                "Read the copied native goal and confirm its recorded source location "
                "and assigned worker.",
                str(stale_run.artifact),
            )
        except ProofBuildMismatchError as error:
            print(error)
        return delivered, closed, stale, stale_run.commander.comms.goals.todos.get(stale.id)

    @classmethod
    def checks(
        cls, run: LifecycleRun, rows: tuple[Todo, ...], native: tuple[ToolResult, ...], output: str
    ) -> tuple[tuple[str, bool], ...]:
        delivered, closed, stale, after = rows
        assert isinstance(delivered.order, DeliveredProblemOrder)
        assert isinstance(stale.order, DeliveredProblemOrder)
        current_build = delivered.order.delivery.proof_build
        older_build = stale.order.delivery.proof_build
        assert isinstance(current_build, RecordedBuildIdentity)
        assert isinstance(older_build, RecordedBuildIdentity)
        return (
            ("installed proof closes", closed.order.accepted and not closed.state.open),
            (
                "build recorded durably",
                FieldCodec.decode(Todo, FieldCodec.encode(stale)) == stale,
            ),
            ("older proof refused without mutation", stale == after and after.state.open),
            (
                "both revisions named",
                all(
                    revision in output
                    for revision in (
                        current_build.core,
                        current_build.toad,
                        older_build.core,
                        older_build.toad,
                    )
                ),
            ),
            ("native close returned", bool(native)),
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
