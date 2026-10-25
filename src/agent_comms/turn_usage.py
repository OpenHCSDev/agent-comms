"""Provisional versus confirmed context usage."""

from __future__ import annotations

from dataclasses import dataclass, field

from .pi_counts import PiCount, UnreportedCount


@dataclass
class UsageAccount:
    used: PiCount = field(default_factory=UnreportedCount)
    size: PiCount = field(default_factory=UnreportedCount)
    confirmed: PiCount = field(default_factory=UnreportedCount)
    provisional: bool = False

    def invalidate(self) -> None:
        self.used = self.confirmed = UnreportedCount()
        self.provisional = False

    def observe(self, tokens: PiCount) -> None:
        self.used = tokens
        self.provisional = True

    def confirm(self, tokens: PiCount) -> None:
        self.used = self.confirmed = tokens
        self.provisional = False

    def rollback(self) -> None:
        self.used = self.confirmed
        self.provisional = False
