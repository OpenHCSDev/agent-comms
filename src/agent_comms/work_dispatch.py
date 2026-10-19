"""Checked work starts through Core's existing order and native lifecycle owners.

A phase declares an existing PendingProblemOrder, not a second task format. Work
without that declaration stays visible; readiness is not permission to invent an
acceptance check. Failed startup state stays retained for explicit disposition.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .goal_actions import (
    CreateProblemGoalAction,
    GoalActionContext,
    OrderGoalAction,
    ProblemOrderGoalAction,
)
from .native_settings import NativeThreadDefaults
from .phase_tasks import PhaseTask
from .recorded_time import RecordedEventTime
from .storage_rules import CheckoutStorageEffect
from .thread_status import StoppedThreadStatus

if TYPE_CHECKING:
    from .comms import Comms
    from .plan_phases import PlanPhase
    from .threads import Thread
    from .todos import PendingProblemOrder, Todo


@dataclass(frozen=True)
class WorkDispatch:
    """One checked native order: allocate its unit, record its claim, then start it."""

    action: ProblemOrderGoalAction
    revision: int

    def start(self, comms: Comms) -> Thread:
        defaults = NativeThreadDefaults()
        # A concurrent rotation of this same row contends on one git checkout,
        # not on two newly generated unit names. Never adopt a partial attempt.
        name = f"unit-{self.action.problem.lower()}-r{self.revision}"
        storage = comms.registry.storage_rule
        checkout = storage.order_checkout(name, self.action.problem)
        candidate = comms.registry.new_thread(
            name,
            frozenset(),
            str(checkout),
            model=defaults.model,
            thinking_level=defaults.thinking_level,
        )
        self.action.request_ref(GoalActionContext(comms.goals, candidate))
        comms.registry.require_declaration_available(candidate, StoppedThreadStatus())
        CheckoutStorageEffect(checkout).check(storage, candidate)
        subprocess.run(
            ("git", "-C", self.action.order.check.directory, "fetch", "origin", "main"),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        subprocess.run(
            (
                "git",
                "-C",
                self.action.order.check.directory,
                "worktree",
                "add",
                "-b",
                f"auto/{name}",
                str(checkout),
                "origin/main",
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        location = candidate.runtime.new_location(comms.root, checkout)
        child = comms.registry.declare(
            replace(candidate, session_file=location.read_key),
            StoppedThreadStatus(),
        )
        # A failed publication leaves a stopped unit and its checkout, not an
        # untracked running task. A failed/uncertain start never releases the claim.
        comms.goals.update_goal(child.name, self.action)
        comms.owners.start(child.name)
        return comms.registry.require(child.name)

    @classmethod
    def reissue(cls, comms: Comms, commander: Thread, todo: Todo) -> None:
        """Retained checked work needs no already-idle unit."""
        current = comms.goals.todos.get(todo.id)
        current.require_revision(todo.revision)
        if current.holder or not current.pending:
            return
        action = OrderGoalAction(
            problem=current.id,
            expected_revision=current.revision,
            order=current.order.reissued(time.time()),
            request=current.goal.request(),
            deadline=current.deadline,
            commander=commander.name,
        )
        cls(action, revision=current.revision).start(comms)


@dataclass(frozen=True)
class CheckedPhaseTask(PhaseTask):
    """An explicitly authored task; its order owns source, check, edits and run."""

    problem: str
    repo: str
    text: str
    order: PendingProblemOrder
    deadline_seconds: float
    instruction = "Core starts the declared checked task in its own unit checkout"

    def __post_init__(self) -> None:
        if not 0 < self.deadline_seconds <= 24 * 60 * 60:
            raise ValueError("A phase task needs a positive deadline of at most one day.")

    def start(self, comms: Comms, commander: Thread, phase: type[PlanPhase]) -> None:
        history = comms.goals.todos.history_for_commander(commander.incarnation)
        if not phase.ready(history) or phase.ordered(history):
            return
        # An existing unowned row belongs to UnownedWork, which reissues that
        # row's actual order instead of replacing it with a phase template.
        if any(todo.state.open for todo in phase.serving(history)):
            return
        now = time.time()
        order = replace(self.order.reissued(now), serves=phase)
        action = CreateProblemGoalAction(
            problem=self.problem,
            repo=self.repo,
            text=self.text,
            order=order,
            request=order.request(commander, (self.problem,)),
            commander=commander.name,
            deadline=RecordedEventTime(now + self.deadline_seconds),
        )
        WorkDispatch(action, revision=0).start(comms)

    def done(self, todos: Iterable[Todo]) -> bool:
        return any(
            todo.id == self.problem and not todo.state.open and todo.order.accepted
            for todo in todos
        )
