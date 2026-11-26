"""The compaction events both harnesses report, independent of call-record storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from agent_comms.messaging.channel_targets import CommsTag
from agent_comms.messaging.messages import MessageRole, NoticeRole
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.storage.recorded_time import RecordedEventTime


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
    def body(self) -> str:
        clock = self.at.clock()
        return self._body_step(clock)

    @abstractmethod
    def _body_step(self, clock) -> str:
        ...


class IgnoredCompactionDeal(CompactionNotice):
    def _body_step(self, clock) -> str:
        return (f"{self.owner.name} ignored the compaction deal at {clock}: "
                f"first refused call was {self.tool}; call #{self.call_number}, refused.")


class StartedRuleReading(CompactionNotice):
    def _body_step(self, clock) -> str:
        return (f"{self.owner.name} finally read its first dealt rule at {clock}: "
                f"{self.tool}, call #{self.call_number} since this compaction.")
