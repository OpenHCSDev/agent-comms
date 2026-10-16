"""Core's default model selection for newly created native threads."""

from dataclasses import dataclass
from typing import ClassVar

from .cli_options import JsonFlagRecord
from .pi_vocabulary import HighThinkingLevel, ThinkingLevel


@dataclass(frozen=True)
class NativeThreadDefaults(JsonFlagRecord):
    """Creation bindings and CLI defaults borrow this declared selection."""

    value_flag: ClassVar[str] = "--native-settings"
    model: str = "openai-codex/gpt-6.1-sol"
    thinking_level: type[ThinkingLevel] = HighThinkingLevel
