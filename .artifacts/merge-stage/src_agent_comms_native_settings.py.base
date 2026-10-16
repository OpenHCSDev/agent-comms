"""Core's default model selection for newly created native threads."""

from dataclasses import dataclass

from .pi_vocabulary import HighThinkingLevel, ThinkingLevel


@dataclass(frozen=True)
class NativeThreadDefaults:
    """Creation bindings and CLI defaults borrow this declared selection."""

    model: str = "openai-codex/gpt-6.1-sol"
    thinking_level: type[ThinkingLevel] = HighThinkingLevel
