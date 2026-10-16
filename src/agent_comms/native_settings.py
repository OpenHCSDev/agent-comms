"""Declared native defaults passed explicitly through the public JSON codec."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import ClassVar

from .cli_options import CliRecord, JsonFlagRecord, option
from .launch_runtime import LaunchRuntime
from .pi_vocabulary import HighThinkingLevel, ThinkingLevel

@dataclass(frozen=True)
class NativeThreadDefaults(CliRecord, JsonFlagRecord):
    """Creation defaults and one owner's explicit native launch selection."""

    binary: str = option("--agent-bin")
    arguments: tuple[str, ...] = option("--agent-args", default=(),
                                      normalize=lambda value: tuple(shlex.split(value)))
    model: str = "openai-codex/gpt-6.1-sol"
    thinking_level: type[ThinkingLevel] = HighThinkingLevel
    value_flag: ClassVar[str] = "--native-settings"

    def __post_init__(self) -> None:
        if not self.binary.strip():
            raise ValueError("Native launch binary cannot be empty")

    @classmethod
    def from_runtime(cls, runtime: LaunchRuntime) -> NativeThreadDefaults:
        """Bind creation defaults to the caller's already selected installation."""
        return cls(binary=runtime.native_entrypoint)
