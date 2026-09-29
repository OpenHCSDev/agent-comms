"""Independent owner-local scheduling barriers, using the shared rule family."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .reservation_rules import ReservationRule, RuleCheck

if TYPE_CHECKING:
    from .input_drain import InputDrain


@dataclass(frozen=True, kw_only=True)
class GoalScheduleCheck(RuleCheck):
    session_id: str
    inputs: 'InputDrain'
    turn_busy: Callable[[str], bool]


class ClosingGoalScheduleRule(ReservationRule):
    check_type = GoalScheduleCheck
    explanation = "Input draining is closing."

    def violated(self, check):
        return check.inputs.closing


class OccupiedGoalScheduleRule(ReservationRule):
    check_type = GoalScheduleCheck
    explanation = "The session still owns a running turn."

    def violated(self, check):
        return check.turn_busy(check.session_id)


class NativeInboxGoalScheduleRule(ReservationRule):
    check_type = GoalScheduleCheck
    explanation = "A live native inbox still owns pending input."

    def violated(self, check):
        return check.session_id in check.inputs.backend_inboxes


class PendingWakeGoalScheduleRule(ReservationRule):
    check_type = GoalScheduleCheck
    explanation = "A previously scheduled wake has not finished."

    def violated(self, check):
        wake = check.inputs.wake_tasks.get(check.session_id)
        return wake is not None and not wake.done()


class PendingTurnGoalScheduleRule(ReservationRule):
    check_type = GoalScheduleCheck
    explanation = "Already queued turns have priority over goal continuation."

    def violated(self, check):
        return bool(check.inputs.pending_turns.get(check.session_id))
