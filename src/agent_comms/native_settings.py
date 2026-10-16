"""Declared native defaults passed explicitly through the public JSON codec."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import ClassVar

from .cli_options import CliRecord, JsonFlagRecord, option
from .native_arguments import NativeArguments
from .pi_vocabulary import HighThinkingLevel, ThinkingLevel

@dataclass(frozen=True)
class NativeThreadDefaults(CliRecord, JsonFlagRecord):
    """Creation defaults and one owner's explicit native launch selection."""

    arguments: tuple[str, ...] = option("--agent-args", default=(),
                                      normalize=lambda value: tuple(shlex.split(value)))
    model: str = "openai-codex/gpt-6.1-sol"
    thinking_level: type[ThinkingLevel] = HighThinkingLevel
    value_flag: ClassVar[str] = "--native-settings"
    isolation_arguments: ClassVar[tuple[str, ...]] = (
        "--no-extensions", "--no-skills", "--no-context-files",
    )

    @classmethod
    def offline(cls, *, arguments: tuple[str, ...] = ()) -> NativeThreadDefaults:
        return cls(arguments=("--offline", *cls.isolation_arguments, *arguments))

    @property
    def parsed_arguments(self) -> NativeArguments:
        return NativeArguments.parse(self.arguments)
