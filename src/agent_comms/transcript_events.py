"""Saved presentation facts: each declaration carries only its valid fields."""

from __future__ import annotations

import json
from abc import abstractmethod
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar
from pydantic import JsonValue
from urllib.parse import quote

from . import tool_declaration
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .messages import Message
from .message_reference import MessageReference
from .routing import MessageRoute, TurnRouting
from .tool_results import SENT_MESSAGE_MIME, ToolDiff, ToolText
from .transcript_merge import EventMerge, StreamingMerge

if TYPE_CHECKING:
    from .importing import ImportedMessage


@dataclass(frozen=True, kw_only=True)
class TranscriptEvent(EventMerge, DeclaredFamily, affix="Transcript"):
    routing = None
    source = None
    timestamp = None

    def as_context(self) -> TranscriptEvent:
        """Non-text native evidence retains its own presentation declaration."""
        return self

    text_size: ClassVar[int] = 0
    native_inputs: ClassVar[frozenset[str]] = frozenset()
    incoming_sources: ClassVar[tuple[MessageReference, ...]] = ()

    def with_native_input(self, native_id: str | None) -> TranscriptEvent:
        return self

    @property
    @abstractmethod
    def routed(self) -> bool:
        return False

    def imported(self) -> ImportedMessage | None:
        """This event as portable text in an imported conversation; activity is not conversation."""
        return None


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


@dataclass(frozen=True, kw_only=True)
class RecordedTranscript(TranscriptEvent):
    routing: TurnRouting | None = None
    source: MessageReference | None = None
    timestamp: float | None = None


class TextTranscript(TranscriptEvent):

    def as_context(self) -> ContextTranscript:
        return ContextTranscript(self.text, timestamp=self.timestamp)

    starts_activity = True

    @property
    def text_size(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class NativeTextTranscript(TextTranscript, RecordedTranscript):
    text: str = ""


class MarkdownTranscript(NativeTextTranscript):
    """Text whose body uses the shared Markdown preparation owner."""


class LiveTextTranscript(StreamingMerge, MarkdownTranscript):
    """Text that can continue streaming inside an already mounted presentation."""


class SilentTranscript:
    routed = False


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
    routed = True


@dataclass(frozen=True, kw_only=True)
class IncomingTranscript(TextTranscript):
    message: Message
    reader: str

    @property
    def text(self):
        return self.message.body

    @property
    def timestamp(self):
        return self.message.timestamp

    @property
    def source(self):
        return self.message.reference

    routed = True

    @property
    def routing(self):
        return TurnRouting((self.source,))

    @property
    def incoming_sources(self) -> tuple[MessageReference, ...]:
        return (self.source,)


@dataclass(frozen=True)
class UserTranscript(NativeTextTranscript):
    native_id: str | None = field(default=None, kw_only=True)

    def with_native_input(self, native_id: str | None) -> UserTranscript:
        return replace(self, native_id=native_id)

    @property
    def native_inputs(self) -> frozenset[str]:
        return frozenset((self.native_id,)) if self.native_id is not None else frozenset()

    @property
    def routed(self) -> bool:
        return self.routing is not None and bool(self.routing.requests)

    def imported(self) -> ImportedMessage:
        from .importing import ImportedRequest

        return ImportedRequest(self.text)


class AssistantTranscript(AgentTextTranscript):
    def imported(self) -> ImportedMessage:
        from .importing import ImportedReply

        return ImportedReply(self.text)


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

    def matches(self, message: Message) -> bool:
        return self == type(self).from_message(message)

    def core_message(self, source) -> Message:
        """Read the actual original; a stored report cannot invent its missing kind or role."""
        original = source.sent_messages((self.source,)).get(self.source)
        if original is not None and self.matches(original):
            return original
        raise ValueError("Sent report no longer matches its original assigned wire message")


class ThinkingTranscript(SilentTranscript, LiveTextTranscript):
    @property
    def starts_activity(self) -> bool:
        return bool(self.text.strip())


class ContextTranscript(SilentTranscript, NativeTextTranscript):
    def imported(self) -> ImportedMessage:
        from .importing import ImportedInput

        return ImportedInput(self.text)


@dataclass(frozen=True, kw_only=True)
class ToolTranscript(SilentTranscript, RecordedTranscript):
    tool_call_id: str
    tool_name: str

    @property
    def title(self) -> str:
        return tool_declaration.ToolDeclaration.caption(self.tool_name, {})

    @property
    def kind(self) -> str:
        return tool_declaration.ToolDeclaration.for_name(self.tool_name).acp_kind


@dataclass(frozen=True, kw_only=True)
class ToolStartTranscript(ToolTranscript):
    raw_input: dict[str, JsonValue] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return tool_declaration.ToolDeclaration.caption(self.tool_name, self.raw_input)

    def imported(self) -> ImportedMessage:
        from .importing import ImportedInput

        return ImportedInput(f"[Historical tool call: {self.tool_name}]\n"
                             + json.dumps(self.raw_input, ensure_ascii=False))


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

    def imported(self) -> ImportedMessage:
        from .importing import ImportedInput

        outcome = "" if self.ok else ", failed"
        return ImportedInput(f"[Historical tool result{outcome}]\n{self.text}")

    text: str = ""
    ok: bool = True
    diff: ToolDiff | None = field(default=None, metadata={"wire_omit_default": True})
    sent_message: SentTranscript | None = field(default=None, metadata={"wire_omit_default": True})


@dataclass(frozen=True)
class TurnDigest:
    """One turn's events in their recorded order, read from the unit's own records."""

    turn_id: str
    events: tuple[TranscriptEvent, ...]
