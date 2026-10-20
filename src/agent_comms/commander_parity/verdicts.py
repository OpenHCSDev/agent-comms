"""Judge/native verdicts on actual native edits in LifecycleRun's disposable bus."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from ..active_route import resolve_comms_route
from ..commander.judge import BackVerb, Edit, OkVerb, VerdictVerb
from ..todos import AcceptedVerdict, SendBackVerdict, Todo, ToolCallVerdict
from ..tools import CommsVerdictTool, TodoChanged
from ..turn_context import GoalSegment
from . import ParityComparison
from .lifecycle import LifecycleRun


@dataclass(frozen=True)
class VerdictEffect:
    call_id: str
    verdicts: int
    todo_revisions: int
    strikes: int
    goal_revisions: int
    goal_transitions: int
    goal_retained: bool
    goal_state_retained: bool
    progress: str
    native_calls: int
    native_result_retained: bool
    strike_matches_verdict_event: bool
    declared_verdict_recorded: bool
    goal_transition_retained: bool


class VerdictCase(ABC):
    changes: ClassVar[int]
    native_owner: ClassVar[type[ToolCallVerdict]]

    @classmethod
    @abstractmethod
    def replacement(cls, run: LifecycleRun) -> str: ...

    @classmethod
    @abstractmethod
    def command(cls, run: LifecycleRun, todo: Todo, edit: Edit) -> VerdictVerb: ...

    @staticmethod
    def judge(run: LifecycleRun, command: VerdictVerb, todo: Todo, edit: Edit) -> object:
        return command.apply(run.commander)

    @staticmethod
    def native(run: LifecycleRun, command: VerdictVerb, todo: Todo, edit: Edit) -> object:
        return run.commander.call(
            CommsVerdictTool(
                problem=todo.id, operation=command.operation(run.commander, todo, edit.id)
            )
        )

    @classmethod
    def capture(
        cls, call_id: str, invoke: Callable[[LifecycleRun, VerdictVerb, Todo, Edit], object]
    ) -> VerdictEffect:
        with LifecycleRun.open() as run, contextlib.redirect_stdout(io.StringIO()):
            before = run.create()
            worker = run.commander.comms.registry.require(run.worker)
            goal = worker.goal.require()
            history = len(run.commander.comms.goals.goal_history(run.worker, goal_id=goal.id))
            for path in (Path(worker.session_file), run.decisions, Path(worker.worktree)):
                path.resolve().relative_to(run.commander.root.resolve())
            package = resolve_comms_route().launch().require_private().validate()
            subprocess.run(
                (
                    "node",
                    "--no-global-search-paths",
                    str(Path(__file__).with_name("verdict_edit.mjs")),
                    str(package.path),
                    worker.session_file,
                    str(run.decisions),
                    call_id,
                    cls.replacement(run),
                ),
                cwd=worker.worktree,
                env=package.node_environment(dict(os.environ)),
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            edits = Edit.unjudged(run.commander.comms, run.commander.commander)
            if len(edits) != 1 or edits[0].id != call_id:
                raise ValueError("The copied unit must expose exactly its one real native edit")
            edit = edits[0]
            invoke(run, cls.command(run, before, edit), before, edit)
            saved = run.commander.comms.goals.todos.get(before.id)
            worker = run.commander.comms.registry.require(run.worker)
            after = worker.goal.require()
            native = tuple(
                (item.request, item.result)
                for item in run.commander.invocations
                if isinstance(item.request, CommsVerdictTool)
            )
            if len(native) != 1 or native[0][0].operation.tool_call_id != call_id:
                raise ValueError("The native verdict must judge this one original copied tool call")
            request, result = native[0]
            transitions = run.commander.comms.goals.goal_history(run.worker, goal_id=goal.id)[
                history:
            ]
            return VerdictEffect(
                request.operation.tool_call_id,
                len(saved.verdicts) - len(before.verdicts),
                saved.revision - before.revision,
                saved.strike_count - before.strike_count,
                after.revision - goal.revision,
                len(transitions),
                after.id == goal.id,
                after.state == goal.state,
                GoalSegment.capture(worker, after, run.commander.comms.goals).progress,
                len(native),
                result == TodoChanged(saved, saved.strike_count)
                and request.operation in saved.verdicts,
                all(
                    strike.at == request.operation.at
                    and strike.previous == request.operation.previous
                    for strike in saved.strikes[len(before.strikes) :]
                ),
                isinstance(request.operation, cls.native_owner),
                all(entry.before == goal and entry.after == after for entry in transitions),
            )

    @classmethod
    def valid(cls, effect: VerdictEffect) -> bool:
        return (
            effect.verdicts == effect.todo_revisions == effect.native_calls == 1
            and effect.strikes == effect.goal_revisions == effect.goal_transitions == cls.changes
            and effect.goal_retained
            and effect.goal_state_retained
            and effect.native_result_retained
            and effect.strike_matches_verdict_event
            and effect.declared_verdict_recorded
            and effect.goal_transition_retained
        )


class AcceptedCase(VerdictCase):
    changes = 0
    native_owner = AcceptedVerdict

    @classmethod
    def replacement(cls, run: LifecycleRun) -> str:
        return "[]"

    @classmethod
    def command(cls, run: LifecycleRun, todo: Todo, edit: Edit) -> OkVerb:
        return OkVerb(
            ids=(f"{edit.id}:{edit.code()}",),
            why="The native edit removes the copied workflow decision.",
            serves="Remove the copied workflow decision",
            lines=(edit.hunks[0].new_text,),
        )


class SendBackCase(VerdictCase):
    changes = 1
    native_owner = SendBackVerdict

    @classmethod
    def replacement(cls, run: LifecycleRun) -> str:
        return json.dumps([run.worker, run.worker])

    @classmethod
    def command(cls, run: LifecycleRun, todo: Todo, edit: Edit) -> BackVerb:
        return BackVerb(
            ids=(f"{edit.id}:{edit.code()}",),
            msg=todo.messages[-1].message_id,
            lines=(edit.hunks[0].new_text,),
            why="The edit duplicates the copied workflow decision instead of removing it.",
            roast=(
                "This is stupid because the edit keeps two copies of the same decision "
                "instead of deleting it."
            ),
            fix="Remove the copied decision and let the existing check measure its absence.",
            pattern="AGENT-2",
            fact="the copied workflow decision collection",
            at=f"{run.decisions}:1",
            derive="delete-copy",
            by=f"{Path(__file__).resolve()}:1",
        )


@dataclass(frozen=True)
class VerdictComparison(ParityComparison):
    accepted_current: VerdictEffect
    accepted_native: VerdictEffect
    back_current: VerdictEffect
    back_native: VerdictEffect

    @property
    def passed(self) -> bool:
        return (
            self.accepted_current == self.accepted_native
            and self.back_current == self.back_native
            and AcceptedCase.valid(self.accepted_native)
            and SendBackCase.valid(self.back_native)
        )

    def describe(self) -> str:
        return (
            f"accepted current/native equal={self.accepted_current == self.accepted_native}; "
            f"send-back current/native equal={self.back_current == self.back_native}; "
            f"accepted={self.accepted_native!r}; send-back={self.back_native!r}"
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
