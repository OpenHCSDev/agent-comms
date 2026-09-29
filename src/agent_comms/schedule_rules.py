"""Independent owner-local scheduling barriers, using the shared rule family."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .reservation_rules import ReservationRule, RuleCheck

if TYPE_CHECKING:
    from .input_drain import InputDrain


@dataclass(frozen=True, kw_only=True)
class SessionScheduleCheck(RuleCheck):
    session_id: str
    inputs: 'InputDrain'


@dataclass(frozen=True, kw_only=True)
class GoalScheduleCheck(SessionScheduleCheck):
    turn_busy: Callable[[str], bool]


class WakeScheduleCheck(SessionScheduleCheck):
    pass


class ClosingScheduleRule(ReservationRule):
    check_type = SessionScheduleCheck
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
