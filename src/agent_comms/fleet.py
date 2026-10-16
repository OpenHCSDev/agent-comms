"""Order progress derives from its Goal and canonical ActivityLog observation."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .activity import ObservedActivity
from .declared_family import DeclaredFamily
from .field_codec import projected
from .goals import Goal


@dataclass(frozen=True)
class UnitObservation:
    goal: Goal
    activity: ObservedActivity
    observed_at: float

    @property
    def open_goal(self):
        return not self.goal.state.terminal

    @projected(view="wire")
    def progress(self) -> UnitProgress:
        return UnitProgress.select(self)


@dataclass(frozen=True)
class UnitProgress(DeclaredFamily, affix="UnitProgress"):
    priority: ClassVar[int]
    notifies_commander: ClassVar[bool] = False
    summary: ClassVar[str]

    @projected(view="wire", name="summary")
    def display_summary(self) -> str:
        return self.summary

    @classmethod
    @abstractmethod
    def matches(cls, entry: UnitObservation) -> bool: ...

    @classmethod
    def select(cls, entry: UnitObservation):
        for member in sorted(cls.members_with(cls), key=lambda member: member.priority):
            if member.matches(entry):
                return member()
        raise RuntimeError("UnitProgress has no matching declaration")


class DoneUnitProgress(UnitProgress):
    priority = 0
    notifies_commander = True
    summary = "Works in app"

    @classmethod
    def matches(cls, entry):
        return entry.goal.delivery_complete


class OverdueUnitProgress(UnitProgress):
    priority = 10
    notifies_commander = True
    summary = "Deadline passed without accepted delivery"

    @classmethod
    def matches(cls, entry):
        return entry.open_goal and entry.goal.deadline.overdue(entry.observed_at, entry.goal.delivery_time)


class SpinningUnitProgress(UnitProgress):
    priority = 30
    summary = "Sent back at least twice without WorksInApp"

    @classmethod
    def matches(cls, entry):
        return entry.open_goal and entry.goal.author.revision_count >= 2


class MovingUnitProgress(UnitProgress):
    priority = 40
    summary = "Working"

    @classmethod
    def matches(cls, entry):
        return entry.activity.state.busy


class IdleWithOpenGoalUnitProgress(UnitProgress):
    priority = 50
    notifies_commander = True
    summary = "Idle with an open goal"

    @classmethod
    def matches(cls, entry):
        return entry.open_goal


class IdleUnitProgress(UnitProgress):
    priority = 100
    summary = "No open goal"

    @classmethod
    def matches(cls, entry):
        return True
