"""ACP-facing effects of turn orchestration."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from . import agent_events as events
from .routing import MessageRoute
from .transcript_updates import TranscriptUpdate


class TurnEffects(ABC):
    @abstractmethod
    async def _emit_event(
        self,
        session_id: str,
        event: events.AgentEvent | TranscriptUpdate,
        client: Any = None,
        *,
        turn_id: str | None = None,
        route: MessageRoute | None = None,
    ) -> None: ...

    @abstractmethod
    def _debug_log(self, message: str) -> None: ...

    @staticmethod
    @abstractmethod
    def _prompt_text(prompt: list[Any]) -> str: ...
