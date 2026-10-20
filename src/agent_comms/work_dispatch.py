"""Core assigns checked work to its unit, then starts it through the native lifecycle.

A phase declares an existing PendingProblemOrder, not a second task format. Work
without that declaration stays visible; readiness is not permission to invent an
acceptance check. Failed startup state stays retained for explicit disposition.
"""

from __future__ import annotations

import math
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .goal_actions import (
    CreateProblemGoalAction,
    GoalActionContext,
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
    """Allocate a unit, assign its checked native order, then start it."""

    action: ProblemOrderGoalAction
    revision: int

    @staticmethod
    def name_for(problem: str, revision: int) -> str:
        return f"unit-{problem.lower()}-r{revision}"

    @property
    def name(self) -> str:
        return self.name_for(self.action.problem, self.revision)

    def publish(self, comms: Comms, child: Thread) -> Thread:
        """Assign the native order before its unit can begin work."""
        comms.goals.update_goal(child.name, self.action)
        return comms.registry.require(child.name)

    def start(self, comms: Comms) -> Thread:
        defaults = NativeThreadDefaults()
        # A concurrent rotation contends on one checkout. Never adopt a partial attempt.
        name = self.name
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
        # untracked running task. A failed/uncertain start never releases its order.
        self.publish(comms, child)
        comms.owners.start(child.name)
        return comms.registry.require(child.name)


@dataclass(frozen=True)
class ReusedWorkDispatch(WorkDispatch):
    """Publish to an existing unit without allocating or starting another process."""

    unit: Thread

    @property
    def name(self) -> str:
        return self.unit.name

    def start(self, comms: Comms) -> Thread:
        return self.publish(comms, comms.registry.require(self.name))


@dataclass(frozen=True)
class CheckedPhaseTask(PhaseTask):
    """An explicitly authored task; its order owns source, check, edits and run."""

    problem: str
    repo: str
    text: str
    order: PendingProblemOrder
    deadline_seconds: float
    instruction = "Core assigns the declared checked task to a unit and starts it"

    def __post_init__(self) -> None:
        if not math.isfinite(self.deadline_seconds) or self.deadline_seconds <= 0:
            raise ValueError("A phase task needs a finite positive deadline interval.")

    def start(self, comms: Comms, commander: Thread, phase: type[PlanPhase]) -> None:
        history = comms.goals.todos.history_for_commander(commander.incarnation)
        if not phase.ready(history) or phase.ordered(history):
            return
        # This task's existing row belongs to UnownedWheelSpin. Other phase subtasks
        # neither supply this task's check nor replace its explicit identity.
        if any(todo.id == self.problem for todo in history):
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
