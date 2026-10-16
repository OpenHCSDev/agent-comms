"""Fleet views derive work from the registry and original per-thread records."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .messages import Message
from .presentation import ThreadView
from .tool_activity import ToolActivity


class UnreadCount(DeclaredFamily, affix="UnreadCount"):
    pass


@dataclass(frozen=True)
class KnownUnreadCount(UnreadCount):
    count: int


@dataclass(frozen=True)
class UnavailableUnreadCount(UnreadCount):
    reason: str


class SentMessages(DeclaredFamily, affix="SentMessages"):
    @property
    def timestamps(self) -> tuple[float, ...]:
        return ()


@dataclass(frozen=True)
class KnownSentMessages(SentMessages):
    messages: tuple[Message, ...]

    @property
    def timestamps(self):
        return tuple(message.timestamp for message in self.messages)


@dataclass(frozen=True)
class UnavailableSentMessages(SentMessages):
    reason: str


@dataclass(frozen=True)
class FleetEntry:
    view: ThreadView
    tool: ToolActivity
    unread: UnreadCount
    sent: SentMessages
    observed_at: float

    @property
    def goal(self):
        return self.view.thread.goal

    @property
    def open_goal(self):
        return self.goal is not None and not self.goal.state.terminal

    @property
    def busy_since(self):
        turn = self.view.thread.active_turn
        return turn.started_at if turn is not None else self.view.activity.timestamp

    @property
    def last_activity(self):
        return max((self.view.activity.timestamp, self.view.last_seen,
                    *self.tool.timestamps, *self.sent.timestamps))

    @property
    def progress(self):
        return UnitProgress.select(self)

    def to_wire(self):
        progress = self.progress
        return {
            "thread": self.view.thread.name,
            "goal": FieldCodec.encode(self.goal),
            "progress": {**FieldCodec.encode(progress), "summary": progress.summary},
            "activity": {
                "state": self.view.activity.state.value,
                "since": self.view.activity.timestamp,
                "detail": self.view.activity.detail,
                "inbox": FieldCodec.encode(self.view.activity.readiness),
            },
            "last_tool": {**FieldCodec.encode(self.tool), "detail": self.tool.description},
            "unread": FieldCodec.encode(self.unread),
            "last_sent": FieldCodec.encode(self.sent),
        }


@dataclass(frozen=True)
class UnitProgress(DeclaredFamily, affix="UnitProgress"):
    tool: ToolActivity
    priority: ClassVar[int]
    notifies_commander: ClassVar[bool] = False
    summary: ClassVar[str]

    @classmethod
    @abstractmethod
    def matches(cls, entry: FleetEntry) -> bool: ...

    @classmethod
    def select(cls, entry: FleetEntry):
        for member in sorted(cls.members_with(cls), key=lambda member: member.priority):
            if member.matches(entry):
                return member(entry.tool)
        raise RuntimeError("UnitProgress has no matching declaration")


class DoneUnitProgress(UnitProgress):
    priority = 0
    summary = "Works in app"

    @classmethod
    def matches(cls, entry):
        return (entry.goal is not None and entry.goal.progress.works_in_app
                and entry.goal.author.has_report(entry.goal.report_time))


class OverdueUnitProgress(UnitProgress):
    priority = 10
    notifies_commander = True
    summary = "Deadline passed without a report"

    @classmethod
    def matches(cls, entry):
        return (entry.open_goal and entry.goal.deadline.overdue(entry.observed_at)
                and not entry.goal.author.has_report(entry.goal.report_time))


class ToolSilentUnitProgress(UnitProgress):
    priority = 20
    notifies_commander = True
    summary = "Busy for 30 minutes without a tool call"
    seconds: ClassVar[int] = 30 * 60

    @classmethod
    def matches(cls, entry):
        return (entry.open_goal and entry.view.activity.state.busy
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
        return entry.view.activity.state.busy


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
