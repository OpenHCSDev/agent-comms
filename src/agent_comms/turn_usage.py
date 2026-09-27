"""Provider billing and provisional versus confirmed context usage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import agent_events as events


@dataclass
class UsageAccount:
    used: int | None = None
    size: int | None = None
    confirmed: int | None = None
    provisional: bool = False
    response_index: int = 0
    compaction_recorded: bool = False

    @staticmethod
    def positive_tokens(usage: Any) -> int | None:
        if not isinstance(usage, dict):
            return None
        tokens = usage.get("totalTokens")
        return tokens if type(tokens) is int and tokens > 0 else None

    def invalidate(self) -> None:
        self.used = self.confirmed = None
        self.provisional = False

    def charge(self, usage: dict[str, Any]) -> events.ProviderUsage:
        self.response_index += 1
        return events.ProviderUsage(response_id=str(self.response_index), usage=usage)

    def observe(self, tokens: int) -> None:
        self.used = tokens
        self.provisional = True

    def confirm(self, tokens: int) -> None:
        self.used = self.confirmed = tokens
        self.provisional = False

    def rollback(self) -> None:
        self.used = self.confirmed
        self.provisional = False
