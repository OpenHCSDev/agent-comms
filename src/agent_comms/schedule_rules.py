"""Independent owner-local scheduling barriers, using the shared rule family."""

import asyncio
from functools import partial
from .coordinator import Coordination

from acp import RequestError

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .reservation_rules import ReservationRule, RuleCheck, ReservationViolationError
from .routing import ScheduledTurn
from .errors import RelationViolationError
from .wake import derive_exact_reply_target

if TYPE_CHECKING:
    from .input_drain import InputDrain
    from .turn_runner import TurnRunner


@dataclass(frozen=True, kw_only=True)
class SessionScheduleCheck(RuleCheck):
    session_id: str
    inputs: "InputDrain"


@dataclass(frozen=True, kw_only=True)
class GoalScheduleCheck(SessionScheduleCheck):
    turns: "TurnRunner"


class WakeScheduleCheck(SessionScheduleCheck):
    """Validated wake admission and execution under the existing turn lock.

    A queued wake never carries a stale owner grant across a lock wait. The
    current owner and active goal are read only after acquiring the turn lock.
    """

    def schedule(self) -> None:
        try:
            self.require_valid()
        except ReservationViolationError:
            return
        self.inputs.wake_tasks[self.session_id] = self.inputs.runtime.background(self.run())

    async def run(self) -> None:
        inputs, session_id = self.inputs, self.session_id
        while inputs.pending_turns.get(session_id):
            if ClosingScheduleRule().violated(self):
                return
            async with inputs.effects.turns.turn_locks.setdefault(session_id, asyncio.Lock()):
                snapshot = await Coordination.run_worker(inputs.comms.registry.snapshot)
                owner = snapshot.require(inputs.sessions.require(session_id))
                try:
                    snapshot.status(owner.name).require_running()
                except RelationViolationError:
                    inputs.pending_turns.pop(session_id, None)
                    continue
                pending = inputs.pending_turns.pop(session_id, [])
                goal = owner.active_goal
                if goal is not None:
                    pending = [turn for turn in pending if turn.goal_id == goal.id]
                if not pending:
                    continue
                pending, remaining = ScheduledTurn.take_batch(pending)
                if remaining:
                    inputs.pending_turns[session_id] = remaining
                inputs.effects.turns.turn_tasks[session_id] = asyncio.current_task()  # type: ignore[assignment]
                try:
                    await inputs.effects.turns.run_agent_turn(
                        session_id,
                        inputs.sessions.require(session_id),
                        "\n\n".join(turn.prompt for turn in pending),
                        reply_targets=tuple(
                            dict.fromkeys(
                                target
                                for turn in pending
                                if (target := derive_exact_reply_target(turn.origin))
                            )
                        ),
                        origins=tuple(origin for turn in pending for origin in turn.origins),
                        autonomous_goal=len(pending) == 1 and pending[0].autonomous_goal,
                        dependency_wait_id=pending[0].goal_wait_id,
                    )
                except RequestError as error:
                    if goal is None:
                        raise
                    pending[0].require_dependency_wake(error)
                    await Coordination.run_worker(partial(inputs.comms.goals.block_goal_after_failed_turn,
                        owner.name,
                        started_goal=goal,
                        expected_worktree=owner.worktree,
                        diagnostic=(
                            "Standby wake launch authority unavailable; inspect the UNKNOWN "
                            "input before explicit Retry. No input was replayed."
                        ),
                    ))
                    await inputs.effects.turns.goals.sync_goal_execution(session_id, owner.name)
                finally:
                    inputs.effects.turns.turn_tasks.pop(session_id, None)


class ClosingScheduleRule(ReservationRule):
    check_type = SessionScheduleCheck
    explanation = "Input draining is closing."

    def violated(self, check):
        return check.inputs.closing


class OccupiedGoalScheduleRule(ReservationRule):
    check_type = GoalScheduleCheck
    explanation = "The session still owns a running turn."

    def violated(self, check):
        return check.session_id in check.turns.turn_tasks


class NativeInboxGoalScheduleRule(ReservationRule):
    check_type = GoalScheduleCheck
    explanation = "A live native inbox still owns pending input."

    def violated(self, check):
        return check.session_id in check.inputs.backend_inboxes


class PendingWakeScheduleRule(ReservationRule):
    check_type = SessionScheduleCheck
    explanation = "A previously scheduled wake has not finished."

    def violated(self, check):
        wake = check.inputs.wake_tasks.get(check.session_id)
        return wake is not None and not wake.done()


class PendingTurnGoalScheduleRule(ReservationRule):
    check_type = GoalScheduleCheck
    explanation = "Already queued turns have priority over goal continuation."

    def violated(self, check):
        return bool(check.inputs.pending_turns.get(check.session_id))


class DisabledWakeScheduleRule(ReservationRule):
    check_type = WakeScheduleCheck
    explanation = "Background waking is disabled by the input/runtime configuration."

    def violated(self, check):
        return check.inputs.background_wakes_disabled


class EmptyWakeScheduleRule(ReservationRule):
    check_type = WakeScheduleCheck
    explanation = "No queued turn requires a background wake."

    def violated(self, check):
        return not check.inputs.pending_turns.get(check.session_id)
