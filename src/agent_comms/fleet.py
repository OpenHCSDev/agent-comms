"""Fleet views derive work from the registry and original per-thread records."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .declared_family import DeclaredFamily
from .field_codec import projected
from .messages import Message
from .activity import ObservedActivity
from .threads import Thread
from .goal_progress import WorksInApp
from .tool_activity import ToolActivity


class UnreadCount(DeclaredFamily, affix="UnreadCount"):
    pass


@dataclass(frozen=True)
class KnownUnreadCount(UnreadCount):
    count: int


@dataclass(frozen=True)
class UnreadStoreBusy(UnreadCount):
    pass


@dataclass(frozen=True)
class UnreadProjectionRebuilding(UnreadCount):
    through: int
    size: int


@dataclass(frozen=True)
class SentMessages:
    messages: tuple[Message, ...]

    @property
    def timestamps(self):
        return tuple(message.timestamp for message in self.messages)


@dataclass(frozen=True)
class UnitObservation:
    thread: Thread
    activity: ObservedActivity
    tool: ToolActivity
    observed_at: float

    @property
    def goal(self):
        return self.thread.goal

    @property
    def open_goal(self):
        return self.goal is not None and not self.goal.state.terminal

    @property
    def busy_since(self):
        return self.thread.execution.busy_time(self.thread, self.activity)

    @projected(view="wire")
    def progress(self) -> UnitProgress:
        return UnitProgress.select(self)


@dataclass(frozen=True)
class FleetEntry(UnitObservation):
    unread: UnreadCount
    sent: SentMessages
    last_seen: float

    @projected(view="wire")
    def last_activity(self) -> float:
        return max((self.activity.timestamp, self.last_seen,
                    *self.tool.timestamps, *self.sent.timestamps))


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
    summary = "Works in app"

    @classmethod
    def matches(cls, entry):
        return (entry.goal is not None
                and any(isinstance(delivery, WorksInApp) for delivery in entry.goal.progress.deliveries)
                and entry.goal.author.has_report(entry.goal.report_time))


class OverdueUnitProgress(UnitProgress):
    priority = 10
    notifies_commander = True
    summary = "Deadline passed without a report"

    @classmethod
    def matches(cls, entry):
        return entry.open_goal and entry.goal.deadline.overdue(entry.observed_at, entry.goal.report_time)


class ToolSilentUnitProgress(UnitProgress):
    priority = 20
    notifies_commander = True
    summary = "Busy for 30 minutes without a tool call"
    seconds: ClassVar[int] = 30 * 60

    @classmethod
    def matches(cls, entry):
        return (entry.open_goal and entry.activity.state.busy
                and entry.tool.silent_since(entry.busy_since, entry.observed_at, cls.seconds))


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
