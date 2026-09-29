"""Saved presentation facts: each declaration carries only its valid fields."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .declared_family import DeclaredFamily
from .routing import TurnRouting
from .tool_results import ToolDiff
from .transcript_merge import EventMerge, StreamingMerge


@dataclass(frozen=True, kw_only=True)
class TranscriptEvent(EventMerge, DeclaredFamily, affix="Transcript"):
    routing: TurnRouting | None = None
    # Original journal event time in Unix seconds; None is unrecorded, never now.
    timestamp: float | None = None

    @property
    def text_size(self) -> int:
        return 0

    @property
    @abstractmethod
    def routed(self) -> bool:
        return False


@dataclass(frozen=True)
class TextTranscript(TranscriptEvent):
    text: str = ""

    @property
    def starts_activity(self) -> bool:
        return True

    @property
    def text_size(self) -> int:
        return len(self.text)


class LiveTextTranscript(StreamingMerge, TextTranscript):
    """Text that can continue streaming inside an already mounted presentation."""


class SilentTranscript:
    @property
    def routed(self) -> bool:
        return False


class AgentTextTranscript(LiveTextTranscript):
    """Text that can carry an outgoing route and be updated in place."""

    @property
    def routed(self) -> bool:
        return self.routing is not None and self.routing.reply is not None


class UserTranscript(TextTranscript):
    @property
    def routed(self) -> bool:
        return self.routing is not None and bool(self.routing.requests)


class AssistantTranscript(AgentTextTranscript):
    pass


class NoticeTranscript(AgentTextTranscript):
    pass


class SentTranscript(AgentTextTranscript):
    pass


class ThinkingTranscript(SilentTranscript, LiveTextTranscript):
    @property
    def starts_activity(self) -> bool:
        return bool(self.text.strip())


class ContextTranscript(SilentTranscript, TextTranscript):
    pass


@dataclass(frozen=True, kw_only=True)
class ToolTranscript(SilentTranscript, TranscriptEvent):
    tool_call_id: str
    tool_name: str


@dataclass(frozen=True, kw_only=True)
class ToolStartTranscript(ToolTranscript):
    raw_input: Any = None


@dataclass(frozen=True, kw_only=True)
class ToolEndTranscript(ToolTranscript):
    @property
    def text_size(self) -> int:
        return len(self.text)

    text: str = ""
    ok: bool = True
    diff: ToolDiff | None = field(default=None, metadata={"wire_omit_default": True})
