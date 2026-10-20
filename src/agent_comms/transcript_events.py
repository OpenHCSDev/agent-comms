"""Saved presentation facts: each declaration carries only its valid fields."""

from __future__ import annotations

import json
import typing
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import quote

from pydantic import JsonValue

from agent_comms import field_codec

from . import tool_declaration
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .message_reference import MessageReference
from .messages import Message
from .recorded_time import EventTime
from .routing import MessageRoute, TurnRouting
from .tool_results import SENT_MESSAGE_MIME, ToolDiff, ToolText
from .transcript_merge import EventMerge, StreamingMerge
from .ui_model.message_category import (
    AgentCategory,
    InboundCategory,
    MessageCategory,
    OtherCategory,
    OutboundCategory,
    ThinkingCategory,
    ToolCategory,
    UserCategory,
)

if TYPE_CHECKING:
    from .importing import ImportBuffer, ImportLimits


TRUNCATED = "\n[Import excerpt truncated]"


@dataclass(frozen=True, slots=True)
class ImportedMessage(ABC):
    """One transcript event as portable text in the imported Pi session."""

    body: str
    truncated: bool = False

    def import_to(self, accept: Callable[[ImportedMessage], None]) -> None:
        if self.body.strip():
            accept(self)

    def bounded(self, limits: ImportLimits) -> ImportedMessage:
        cap = min(limits.per_message, limits.characters)
        if len(self.body) <= cap:
            return self
        return replace(self, body=self.body[: max(0, cap - len(TRUNCATED))] + TRUNCATED[:cap], truncated=True)

    def remember(self, buffer: ImportBuffer) -> None:
        """Only a request is the source session's latest request."""

    @abstractmethod
    def pi_message(self, timestamp: int) -> dict[str, object]: ...


class ImportedInput(ImportedMessage):
    """Text the source session's model received: tool calls, results and context."""

    def pi_message(self, timestamp: int) -> dict[str, object]:
        return {"role": "user", "content": [{"type": "text", "text": self.body}], "timestamp": timestamp}


class ImportedRequest(ImportedInput):
    """A person's or a commanding agent's prompt."""

    def remember(self, buffer: ImportBuffer) -> None:
        buffer.latest_request = self.body[: buffer.limits.per_message]


class ImportedReply(ImportedMessage):
    def pi_message(self, timestamp: int) -> dict[str, object]:
        zero = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        return {
            "role": "assistant", "content": [{"type": "text", "text": self.body}], "timestamp": timestamp,
            "api": "openai-responses", "provider": "imported", "model": "historical-context", "stopReason": "stop",
            "usage": {**zero, "totalTokens": 0, "cost": {**zero, "total": 0}},
        }


@dataclass(frozen=True, kw_only=True)
class TranscriptEvent(EventMerge, DeclaredFamily, affix="Transcript"):
    category: ClassVar[type[MessageCategory]]
    position: int | str
    timestamp: float
    routing = None
    source = None

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

    def import_to(self, accept: Callable[[ImportedMessage], None]) -> None:
        """Non-conversation events produce no portable message."""

    def record_call(self, calls: dict[str, OrderCall], since: float) -> None:
        """Only tool events change a unit's calls."""

    def edit_calls(self, thread: str, problem: str, since: float) -> tuple[tool_declaration.EditCall, ...]:
        return ()

    def before(self, at: float) -> bool:
        return self.timestamp < at


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


class TextTranscript(TranscriptEvent):

    def as_context(self) -> ContextTranscript:
        return ContextTranscript(self.text, timestamp=self.timestamp, position=self.position)

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
    position: ClassVar[int]
    routed = True

    @property
    def position(self) -> int:
        return self.source.seq


@dataclass(frozen=True, kw_only=True)
class IncomingTranscript(TextTranscript):
    @property
    def category(self) -> type[MessageCategory]:
        return self.message.role.category

    message: Message
    reader: str
    position: ClassVar[int]
    timestamp: ClassVar[float]

    @property
    def position(self) -> int:
        return self.message.seq

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
    category = UserCategory
    native_id: str | None = field(default=None, kw_only=True)

    def with_native_input(self, native_id: str | None) -> UserTranscript:
        return replace(self, native_id=native_id)

    @property
    def native_inputs(self) -> frozenset[str]:
        return frozenset((self.native_id,)) if self.native_id is not None else frozenset()

    @property
    def routed(self) -> bool:
        return self.routing is not None and bool(self.routing.requests)

    def import_to(self, accept: Callable[[ImportedMessage], None]) -> None:
        ImportedRequest(self.text).import_to(accept)


class AssistantTranscript(AgentTextTranscript):
    @property
    def category(self) -> type[MessageCategory]:
        return OutboundCategory if self.routed else AgentCategory

    def import_to(self, accept: Callable[[ImportedMessage], None]) -> None:
        ImportedReply(self.text).import_to(accept)


class NoticeTranscript(AgentTextTranscript):
    @property
    def category(self) -> type[MessageCategory]:
        return OutboundCategory if self.routed else OtherCategory

    def as_context(self) -> NoticeTranscript:
        # Native failures remain visible notices, including bounded triage.
        return self


@dataclass(frozen=True)
class ForkOriginTranscript(NoticeTranscript):
    """The declared first row before a fork's recorded history."""

    position: ClassVar[int] = -2


@dataclass(frozen=True)
class ForkTaskTranscript(UserTranscript):
    """The declared task row following a fork's origin."""

    position: ClassVar[int] = -1


class SentTranscript(OutgoingRoute, WireTextTranscript):
    """One immutable original wire row, separate from native assistant output."""

    category = OutboundCategory

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
    category = ThinkingCategory

    @property
    def starts_activity(self) -> bool:
        return bool(self.text.strip())


class ContextTranscript(SilentTranscript, NativeTextTranscript):
    category = InboundCategory

    def import_to(self, accept: Callable[[ImportedMessage], None]) -> None:
        ImportedInput(self.text).import_to(accept)


@dataclass(frozen=True, kw_only=True)
class ToolTranscript(SilentTranscript, RecordedTranscript):
    category = ToolCategory
    tool_call_id: str
    tool_name: str

    @property
    def title(self) -> str:
        return tool_declaration.ToolDeclaration.caption(self.tool_name, None)

    @property
    def kind(self) -> str:
        return tool_declaration.ToolDeclaration.for_name(self.tool_name).acp_kind


@dataclass(frozen=True, kw_only=True)
class ToolStartTranscript(ToolTranscript):
    def edit_calls(self, thread: str, problem: str, since: float) -> tuple[tool_declaration.EditCall, ...]:
        if self.before(since) or self.tool is None:
            return ()
        return self.tool.edit_calls(
            self.tool_call_id, EventTime.capture(self.timestamp), thread, problem)

    raw_input: dict[str, JsonValue] = field(default_factory=dict)  # as the CLI sent it, for history
    tool: typing.Annotated[
        tool_declaration.ToolDeclaration | None,
        field_codec.WireOmitDefault(True),
        field_codec.WireBind({"call_id": "tool_call_id"}),
    ] = field(default=None)

    @property
    def title(self) -> str:
        return tool_declaration.ToolDeclaration.caption(self.tool_name, self.tool)

    def record_call(self, calls: dict[str, OrderCall], since: float) -> None:
        """A call recorded before ``since`` is not work since then."""
        if not self.before(since):
            digest = tool_declaration.ToolDeclaration.call_digest(
                self.tool_name, self.tool, self.raw_input)
            calls[self.tool_call_id] = OrderCall(
                self.tool_call_id, self.tool_name, digest, RunningCallState)

    def import_to(self, accept: Callable[[ImportedMessage], None]) -> None:
        ImportedInput(f"[Historical tool call: {self.tool_name}]\n"
                      + json.dumps(self.raw_input, ensure_ascii=False)).import_to(accept)


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

    def record_call(self, calls: dict[str, OrderCall], since: float) -> None:
        if self.tool_call_id in calls:
            calls[self.tool_call_id] = replace(
                calls[self.tool_call_id], state=SucceededCallState if self.ok else FailedCallState)

    def import_to(self, accept: Callable[[ImportedMessage], None]) -> None:
        outcome = "" if self.ok else ", failed"
        ImportedInput(f"[Historical tool result{outcome}]\n{self.text}").import_to(accept)

    text: str = ""
    ok: bool = True
    diff: typing.Annotated[ToolDiff | None, field_codec.WireOmitDefault(True)] = field(default=None)
    sent_message: typing.Annotated[SentTranscript | None, field_codec.WireOmitDefault(True)] = (
        field(default=None)
    )


class CallState(DeclaredFamily, affix="CallState"):
    """Where one tool call stands in its unit's records."""


class RunningCallState(CallState):
    """Started; no result is recorded yet."""


class SucceededCallState(CallState):
    pass


class FailedCallState(CallState):
    pass


@dataclass(frozen=True)
class OrderCall:
    tool_call_id: str
    tool_name: str
    digest: str
    state: type[CallState]


@dataclass(frozen=True)
class TurnDigest:
    """One turn's events in their recorded order, read from the unit's own records."""

    turn_id: str
    events: tuple[TranscriptEvent, ...]

    def before(self, at: float) -> bool:
        """The turn's last recorded event precedes ``at``; an empty turn is never before."""
        return bool(self.events) and self.events[-1].before(at)
