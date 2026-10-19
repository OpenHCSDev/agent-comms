"""Provider billing and provisional versus confirmed context usage."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import agent_events as events
from .pi_payloads import PiUsage
from .pi_counts import PiCount, UnreportedCount


@dataclass
class UsageAccount:
    used: PiCount = field(default_factory=UnreportedCount)
    size: PiCount = field(default_factory=UnreportedCount)
    confirmed: PiCount = field(default_factory=UnreportedCount)
    provisional: bool = False
    response_index: int = 0

    def invalidate(self) -> None:
        self.used = self.confirmed = UnreportedCount()
        self.provisional = False

    def charge(self, usage: PiUsage) -> events.ProviderUsage:
        self.response_index += 1
        return events.ProviderUsage(response_id=str(self.response_index), usage=usage)

    def observe(self, tokens: PiCount) -> None:
        self.used = tokens
        self.provisional = True

    def confirm(self, tokens: PiCount) -> None:
        self.used = self.confirmed = tokens
        self.provisional = False

    def rollback(self) -> None:
        self.used = self.confirmed
        self.provisional = False
