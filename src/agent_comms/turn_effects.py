"""ACP-facing effects of turn orchestration."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from acp.schema import (
    AudioContentBlock, EmbeddedResourceContentBlock, ImageContentBlock,
    ResourceContentBlock, TextContentBlock,
)
from pydantic import JsonValue

type PromptBlock = (
    TextContentBlock | ImageContentBlock | AudioContentBlock
    | ResourceContentBlock | EmbeddedResourceContentBlock | dict[str, JsonValue]
)

if TYPE_CHECKING:
    from .runtime import RuntimeServer
    from .session_lifecycle import SessionLifecycle
    from .turn_runner import TurnRunner


class TurnEffects(ABC):
    _runtime: RuntimeServer
    sessions: SessionLifecycle
    turns: TurnRunner

    @abstractmethod
    def _debug_log(self, message: str) -> None: ...

    @staticmethod
    @abstractmethod
    def _prompt_text(prompt: list[PromptBlock]) -> str: ...
