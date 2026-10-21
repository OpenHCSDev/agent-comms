"""Judge/native head verdicts with copied native edits and controlled gh responses."""

from __future__ import annotations

import contextlib
import io
import json
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from agent_comms.active_route import resolve_comms_route
from agent_comms.launch_root import LaunchRoot
from agent_comms.launch_runtime import ThreadLaunch
from agent_comms.commander.judge import BackVerb, Edit, OkVerb, VerdictVerb
from agent_comms.todos import AcceptedHeadVerdict, HeadVerdict, PullRequests, RejectedHeadVerdict, Todo
from agent_comms.tools import CommsVerdictTool, TodoChanged
from agent_comms.turn_context import GoalSegment
from agent_comms.commander_parity import ParityComparison
from agent_comms.commander_parity.lifecycle import LifecycleObservation, LifecycleRun

NATIVE_EDIT = Path(__file__).with_name("verdict_edit.mjs")


@dataclass(frozen=True)
class VerdictMutation:
    head: str
    verdicts: int
    todo_revisions: int
    strikes: int
    goal_revisions: int
    goal_history_entries: int
    progress: str

    def checks(self, changes: int) -> tuple[tuple[str, bool], ...]:
        return (
            ("one verdict", self.verdicts == 1),
            ("one Todo revision", self.todo_revisions == 1),
            ("strike delta", self.strikes == changes),
            ("Goal revision delta", self.goal_revisions == changes),
            ("committed Goal history delta", self.goal_history_entries == changes),
        )


@dataclass(frozen=True)
class VerdictObservation(LifecycleObservation):
    mutation: VerdictMutation


class VerdictCase(ABC):
    changes: ClassVar[int]
    native_owner: ClassVar[type[HeadVerdict]]

    @classmethod
    @abstractmethod
    def replacement(cls, run: LifecycleRun) -> str: ...

    @classmethod
    @abstractmethod
    def command(cls, run: LifecycleRun, todo: Todo, edit: Edit) -> VerdictVerb: ...

    @staticmethod
    def judge(run: LifecycleRun, command: VerdictVerb, todo: Todo, edit: Edit) -> None:
        command.apply(run.commander)

    @staticmethod
    def native(run: LifecycleRun, command: VerdictVerb, todo: Todo, edit: Edit) -> object:
        request = PullRequests((todo,)).current(todo)
        command.check_diff(request.diff(todo.repo))
        return run.commander.call(CommsVerdictTool(
            problem=todo.id, operation=command.operation(run.commander, todo, request)))

    @classmethod
    def capture(
        cls, call_id: str, invoke: Callable[[LifecycleRun, VerdictVerb, Todo, Edit], object]
    ) -> VerdictObservation:
        with LifecycleRun.open() as run, contextlib.redirect_stdout(io.StringIO()):
            before = run.create()
            worker = run.commander.comms.registry.require(run.worker)
            goal = worker.goal.require()
            history = len(run.commander.comms.goals.goal_history(run.worker, goal_id=goal.id))
            session_file = worker.session_file.require(
                "The copied unit must have its own recorded native session")
            for path in (Path(session_file), run.decisions, Path(worker.worktree)):
                path.resolve().relative_to(run.commander.root.resolve())
            package = resolve_comms_route().launch().require_private().validate()
            child = run.invoke_command(
                (
                    "node",
                    "--no-global-search-paths",
                    str(NATIVE_EDIT),
                    str(package.path),
                    session_file,
                    str(run.decisions),
                    call_id,
                    cls.replacement(run),
                ),
                cwd=worker.worktree,
                env=package.node_environment(LaunchRoot.environment(
                    ThreadLaunch.strip(dict(os.environ)))),
            )
            if not child.outcome.successful:
                raise RuntimeError(
                    f"Native edit {child.outcome.summary}: {child.stderr.decode('utf-8')}"
                )
            edits = Edit.unjudged(run.commander.comms, run.commander.commander)
            if len(edits) != 1 or edits[0].id != call_id:
                raise ValueError("The copied unit must expose exactly its one real native edit")
            edit = edits[0]
            with run.forge(before, '\n'.join(hunk.rendered() for hunk in edit.hunks)):
                before = run.commander.comms.goals.todos.get(before.id)
                invoke(run, cls.command(run, before, edit), before, edit)
            saved = run.commander.comms.goals.todos.get(before.id)
            worker = run.commander.comms.registry.require(run.worker)
            after = worker.goal.require()
            native = tuple(
                (item.request, item.result)
                for item in run.commander.invocations
                if isinstance(item.request, CommsVerdictTool)
            )
            if len(native) != 1:
                raise ValueError("The native verdict must judge exactly one linked current PR head")
            request, result = native[0]
            # goal_history returns committed revisions; its internal tags stay there.
            history_entries = run.commander.comms.goals.goal_history(run.worker, goal_id=goal.id)[
                history:
            ]
            progress = GoalSegment.capture(worker, after, run.commander.comms.goals).progress
            mutation = VerdictMutation(
                request.operation.head,
                len(saved.verdicts) - len(before.verdicts),
                saved.revision - before.revision,
                saved.strike_count - before.strike_count,
                after.revision - goal.revision,
                len(history_entries),
                progress,
            )
            checks = (
                *mutation.checks(cls.changes),
                ("same Goal identity", after.id == goal.id),
                ("same Goal state", after.state == goal.state),
                ("native result retained", result == TodoChanged(saved, saved.strike_count)),
                ("native verdict retained", request.operation in saved.verdicts),
                ("reviewed native edit no longer unjudged",
                 not Edit.unjudged(run.commander.comms, run.commander.commander)),
                (
                    "strike is the verdict event",
                    all(
                        strike.at == request.operation.at
                        and strike.previous == request.operation.previous
                        for strike in saved.strikes[len(before.strikes) :]
                    ),
                ),
                ("declared verdict kind", isinstance(request.operation, cls.native_owner)),
                (
                    "Goal publication retained",
                    all(entry.before == goal and entry.after == after for entry in history_entries),
                ),
                ("native verdict visible in Goal", request.operation.line in progress),
            )
            return VerdictObservation(
                f"{cls.native_owner.__module__}.{cls.native_owner.__qualname__}",
                len(native),
                checks,
                "Copied native history and Todo; controlled gh responses, no live review status changed.",
                mutation,
            )


class AcceptedCase(VerdictCase):
    changes = 0
    native_owner = AcceptedHeadVerdict

    @classmethod
    def replacement(cls, run: LifecycleRun) -> str:
        return "[]"

    @classmethod
    def command(cls, run: LifecycleRun, todo: Todo, edit: Edit) -> OkVerb:
        return OkVerb(
            problem=todo.id,
            why="The native edit removes the copied workflow decision.",
            serves="Remove the copied workflow decision",
            lines=(edit.hunks[0].new_text,),
        )


class SendBackCase(VerdictCase):
    changes = 1
    native_owner = RejectedHeadVerdict
    why: ClassVar[str] = "The edit duplicates the copied workflow decision instead of removing it."

    @classmethod
    def replacement(cls, run: LifecycleRun) -> str:
        return json.dumps([run.worker, run.worker])

    @classmethod
    def command(cls, run: LifecycleRun, todo: Todo, edit: Edit) -> BackVerb:
        return BackVerb(
            problem=todo.id,
            serves="Remove the copied workflow decision",
            lines=(edit.hunks[0].new_text,),
            why=cls.why,
            roast=(
                "This is stupid because the edit keeps two copies of the same decision "
                "instead of deleting it."
            ),
            fix="Remove the copied decision and let the existing check measure its absence.",
            pattern="AGENT-2",
            fact="the copied workflow decision collection",
            at=todo.order.checked().owner.to_wire(),
            derive="delete-copy",
            by=f"{Path(__file__).resolve()}:1",
        )


@dataclass(frozen=True)
class VerdictComparison(ParityComparison):
    accepted_current: VerdictObservation
    accepted_native: VerdictObservation
    back_current: VerdictObservation
    back_native: VerdictObservation

    @property
    def passed(self) -> bool:
        observations = (
            self.accepted_current,
            self.accepted_native,
            self.back_current,
            self.back_native,
        )
        return (
            all(observation.passed for observation in observations)
            and self.accepted_current.mutation == self.accepted_native.mutation
            and self.back_current.mutation == self.back_native.mutation
        )

    def describe(self) -> str:
        return (
            "accepted current/native equal="
            f"{self.accepted_current.mutation == self.accepted_native.mutation}; "
            "send-back current/native equal="
            f"{self.back_current.mutation == self.back_native.mutation}; "
            f"accepted={self.accepted_native.describe()}; send-back={self.back_native.describe()}"
        )

    @classmethod
    def read(cls) -> VerdictComparison:
        call_id = uuid4().hex
        return cls(
            AcceptedCase.capture(call_id, AcceptedCase.judge),
            AcceptedCase.capture(call_id, AcceptedCase.native),
            SendBackCase.capture(call_id, SendBackCase.judge),
            SendBackCase.capture(call_id, SendBackCase.native),
        )
