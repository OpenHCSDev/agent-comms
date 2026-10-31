"""The compaction events both harnesses report, independent of call-record storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .channel_targets import CommsTag
from .messages import MessageRole, NoticeRole
from .recorded_time import RecordedEventTime
from .thread_identity import RecordedThreadIncarnation


@dataclass(frozen=True)
class CompactionNotice(ABC):
    """One event's bound actor, call ordinal and time, with its declared destination."""

    owner: RecordedThreadIncarnation
    tool: str
    call_number: int
    at: RecordedEventTime

    channel: ClassVar[str] = CommsTag.channel()
    role: ClassVar[MessageRole] = NoticeRole()

    @property
    @abstractmethod
    def body(self) -> str: ...


class IgnoredCompactionDeal(CompactionNotice):
    @property
    def body(self) -> str:
        return (f"{self.owner.name} ignored the compaction deal at {self.at.clock()}: "
                f"first refused call was {self.tool}; call #{self.call_number}, refused.")


class StartedRuleReading(CompactionNotice):
    @property
    def body(self) -> str:
        return (f"{self.owner.name} finally read its first dealt rule at {self.at.clock()}: "
                f"{self.tool}, call #{self.call_number} since this compaction.")
