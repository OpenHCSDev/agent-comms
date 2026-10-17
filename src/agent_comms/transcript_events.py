"""Saved presentation facts: each declaration carries only its valid fields."""

from __future__ import annotations

import json
from abc import abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import quote

from . import tool_declaration
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .message_reference import MessageReference
from .routing import MessageRoute, TurnRouting
from .tool_results import SENT_MESSAGE_MIME, ToolDiff, ToolText
from .transcript_merge import EventMerge, StreamingMerge


@dataclass(frozen=True, kw_only=True)
class TranscriptEvent(EventMerge, DeclaredFamily, affix="Transcript"):
    routing: TurnRouting | None = None
    source: MessageReference | None = None
    # Original journal event time in Unix seconds; None is unrecorded, never now.
    timestamp: float | None = None

    def as_context(self) -> TranscriptEvent:
        """Non-text native evidence retains its own presentation declaration."""
        return self

    @property
    def tool_calls(self):
        return ()

    @property
    def text_size(self) -> int:
        return 0

    @property
    def native_inputs(self) -> frozenset[str]:
        return frozenset()

    @property
    def incoming_sources(self) -> tuple[MessageReference, ...]:
        return ()

    def with_native_input(self, native_id: str | None) -> TranscriptEvent:
        return self

    @property
    @abstractmethod
    def routed(self) -> bool:
        return False


@dataclass(frozen=True)
class TranscriptCoverage:
    """The incoming wire sequences and native inputs a run of events carries."""

    events: tuple[TranscriptEvent, ...]

    @property
    def sequences(self) -> frozenset[int]:
        return frozenset(source.seq for event in self.events
                         for source in event.incoming_sources if source.seq > 0)

    @property
    def native_inputs(self) -> frozenset[str]:
        return frozenset(native_id for event in self.events for native_id in event.native_inputs)


@dataclass(frozen=True)
class TextTranscript(TranscriptEvent):
    text: str = ""

    def as_context(self) -> ContextTranscript:
        return ContextTranscript(self.text, timestamp=self.timestamp)

    @property
    def starts_activity(self) -> bool:
        return True

    @property
    def text_size(self) -> int:
        return len(self.text)


class MarkdownTranscript(TextTranscript):
    """Text whose body uses the shared Markdown preparation owner."""


class LiveTextTranscript(StreamingMerge, MarkdownTranscript):
    """Text that can continue streaming inside an already mounted presentation."""


class SilentTranscript:
    @property
    def routed(self) -> bool:
        return False


class OutgoingRoute:
    """Route capability shared by native output and original wire projection."""

    def require_reply_route(self) -> MessageRoute:
        if not self.routed:
            raise ValueError("Outgoing message evidence requires a reply route")
        return self.routing.reply

    @property
    def routed(self) -> bool:
        return self.routing is not None and self.routing.reply is not None


class AgentTextTranscript(OutgoingRoute, LiveTextTranscript):
    """Native output that can continue streaming inside one mounted block."""


@dataclass(frozen=True, kw_only=True)
class WireTextTranscript(MarkdownTranscript):
    """Immutable presentation of one original committed wire record."""

    source: MessageReference = field()

    @property
    def routed(self) -> bool:
        return True


@dataclass(frozen=True, kw_only=True)
class IncomingTranscript(WireTextTranscript):
    route: MessageRoute

    @property
    def incoming_sources(self) -> tuple[MessageReference, ...]:
        return (self.source,)


@dataclass(frozen=True)
class UserTranscript(TextTranscript):
    native_id: str | None = field(default=None, kw_only=True)

    def with_native_input(self, native_id: str | None) -> UserTranscript:
        return replace(self, native_id=native_id)

    @property
    def native_inputs(self) -> frozenset[str]:
        return frozenset((self.native_id,)) if self.native_id is not None else frozenset()

    @property
    def routed(self) -> bool:
        return self.routing is not None and bool(self.routing.requests)


class AssistantTranscript(AgentTextTranscript):
    pass


class NoticeTranscript(AgentTextTranscript):
    def as_context(self) -> NoticeTranscript:
        # Native failures remain visible notices, including bounded triage.
        return self


class SentTranscript(OutgoingRoute, WireTextTranscript):
    """One immutable original wire row, separate from native assistant output."""

    def tool_content(self, tool_call_id):
        """Encode the Send report only at its actual ACP output boundary."""
        return {"type": "content", "content": {
            "type": "resource", "resource": {
                "uri": f"agent-comms:///messages/{self.source.seq}/{quote(self.source.message_id, safe='')}",
                "mimeType": SENT_MESSAGE_MIME,
                "text": json.dumps(FieldCodec.encode(self)),
            },
        }}

    @classmethod
    def from_message(cls, message):
        return cls(message.body, timestamp=message.timestamp, source=message.reference,
                   routing=TurnRouting(reply=MessageRoute(message.sender, (message.target,))))


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

    @property
    def title(self) -> str:
        return tool_declaration.ToolDeclaration.caption(self.tool_name)

    @property
    def kind(self) -> str:
        return tool_declaration.ToolDeclaration.for_name(self.tool_name).acp_kind


@dataclass(frozen=True, kw_only=True)
class ToolStartTranscript(ToolTranscript):
    raw_input: Any = None
    detail: str = field(default="", metadata={"wire_omit_default": True})

    @property
    def title(self) -> str:
        return tool_declaration.ToolDeclaration.caption(self.tool_name, self.detail)

    @property
    def tool_calls(self):
        return (self,)

    def made_before(self, at: float) -> bool:
        """Only a recorded selection time proves the call is older than ``at``."""
        return self.timestamp is not None and self.timestamp < at

    def quotes(self, text: str) -> bool:
        """An exact quoted input string cannot cross JSON field boundaries."""
        return json.dumps(text)[1:-1] in json.dumps(self.raw_input)


@dataclass(frozen=True, kw_only=True)
class ToolEndTranscript(ToolTranscript):
    @staticmethod
    def parts_for(text, diff=None, sent_message=None):
        """The result owner selects its contents once, before transport or paint."""
        if sent_message is not None:
            return (sent_message,)
        return ((diff,) if diff is not None else ()) + ((ToolText(text),) if text else ())

    @property
    def result_parts(self):
        return self.parts_for(self.text, self.diff, self.sent_message)

    @property
    def text_size(self) -> int:
        return len(self.text)

    text: str = ""
    ok: bool = True
    diff: ToolDiff | None = field(default=None, metadata={"wire_omit_default": True})
    sent_message: SentTranscript | None = field(default=None, metadata={"wire_omit_default": True})


@dataclass(frozen=True)
class TurnDigest:
    """One turn's events in their recorded order, read from the unit's own records."""

    turn_id: str
    events: tuple[TranscriptEvent, ...]
