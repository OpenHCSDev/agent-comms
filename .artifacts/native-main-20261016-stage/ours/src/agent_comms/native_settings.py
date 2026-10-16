"""Declared native defaults passed explicitly through the public JSON codec."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .cli_options import CliRecord, JsonFlagRecord, option
from .native_arguments import ExternalArgument, NativeArguments
from .pi_vocabulary import HighThinkingLevel, ThinkingLevel

@dataclass(frozen=True)
class NativeThreadDefaults(CliRecord, JsonFlagRecord):
    """Creation defaults and one owner's explicit native launch selection."""

    arguments: NativeArguments = option("--agent-args", default=NativeArguments(),
                                        normalize=NativeArguments.parse)
    model: str = "openai-codex/gpt-6.1-sol"
    thinking_level: type[ThinkingLevel] = HighThinkingLevel
    value_flag: ClassVar[str] = "--native-settings"
    isolation_arguments: ClassVar[NativeArguments] = NativeArguments.parse((
        "--no-extensions", "--no-skills", "--no-context-files",
    ))

    def startup_arguments(self) -> NativeArguments:
        """Explicit argv selections take precedence over creation defaults."""
        model, thinking = self.arguments.model, self.arguments.thinking
        return self.arguments.with_model(self.model if model is None else model).with_thinking(
            self.thinking_level.declared_name if thinking is None else thinking)

    @classmethod
    def offline(cls, *, arguments: NativeArguments = NativeArguments()) -> NativeThreadDefaults:
        return cls(arguments=NativeArguments((ExternalArgument(("--offline",)),
                                              *cls.isolation_arguments.entries, *arguments.entries)))
