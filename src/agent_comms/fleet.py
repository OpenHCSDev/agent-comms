"""Order progress derives from its Goal and canonical ActivityLog observation."""
from __future__ import annotations

from dataclasses import dataclass
import time

from .activity import ObservedActivity
from .goals import Goal


@dataclass(frozen=True)
class UnitProgress:
    summary: str
    notifies_commander: bool = False

    @classmethod
    def of(cls, goal: Goal | None, activity: ObservedActivity | None) -> UnitProgress:
        if goal is not None:
            if goal.delivery_complete:
                return cls("Works in app", notifies_commander=True)
            if not goal.state.terminal and goal.deadline.overdue(time.time(), goal.delivery_time):
                return cls("Deadline passed without accepted delivery", notifies_commander=True)
        if activity is not None and activity.state.busy:
            return cls("Working")
        if goal is not None and not goal.state.terminal:
            return cls("Idle with an open goal", notifies_commander=True)
        return cls("No open goal")
