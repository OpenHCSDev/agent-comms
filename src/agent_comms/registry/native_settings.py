"""Core's default model selection for newly created native threads."""

from dataclasses import dataclass
from typing import ClassVar

from agent_comms.execution.pi_vocabulary import HighThinkingLevel, ThinkingLevelField
from agent_comms.registry.cli_options import JsonFlagRecord
from agent_comms.registry.thread_model import DeclaredModel
from agent_comms.registry.thread_settings import ThreadSettings


@dataclass(frozen=True)
class NativeThreadDefaults(ThreadSettings, JsonFlagRecord):
    """Creation bindings and CLI defaults borrow this declared selection."""

    value_flag: ClassVar[str] = "--native-settings"
    model: DeclaredModel = DeclaredModel("openai-codex/gpt-6.1-sol")
    thinking_level: ThinkingLevelField = HighThinkingLevel
