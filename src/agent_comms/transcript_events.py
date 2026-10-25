"""Saved presentation facts: each declaration carries only its valid fields."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Annotated, ClassVar
from urllib.parse import quote

from pydantic import JsonValue

from . import tool_declaration
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, InputId, NullMember, PresentMember, WireBind, WireOmitDefault
from .message_reference import MessageReference
from .messages import Message
from .native_tool_call import ToolCallId
from .recorded_time import EventTime, UnrecordedEventTime
from .routing import MessageRoute, NoTurnRouting, Routing, TurnRouting
from .tool_results import SENT_MESSAGE_MIME, NoSentMessage, NoToolDiff, ToolDiff, ToolText
from .transcript_merge import EventMerge, StreamingMerge
from .turn_item import NativeItemId, NativeTurnId
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
    from .transcript_sources import TranscriptSource


TRUNCATED = "\n[Import excerpt truncated]"


@dataclass(frozen=True, slots=True)
class NoRetainedRequest(NullMember):
    def matches(self, latest_request: str) -> bool:
        return False


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

    def retained_request(self) -> ImportedRequest | NoRetainedRequest:
        return NoRetainedRequest()

    @abstractmethod
    def pi_message(self, timestamp: int) -> dict[str, object]: ...


class ImportedInput(ImportedMessage):
    """Text the source session's model received: tool calls, results and context."""

    def pi_message(self, timestamp: int) -> dict[str, object]:
        return {"role": "user", "content": [{"type": "text", "text": self.body}], "timestamp": timestamp}


class ImportedRequest(PresentMember, ImportedInput):
    """A person's or a commanding agent's prompt."""

    def remember(self, buffer: ImportBuffer) -> None:
        buffer.latest_request = self.body[: buffer.limits.per_message]

    def retained_request(self) -> ImportedRequest:
        return self

    def matches(self, latest_request: str) -> bool:
        return self.body == latest_request or (
            self.truncated and latest_request.startswith(self.body.removesuffix(TRUNCATED))
        )


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
    position: int | NativeItemId
    timestamp: float
    routing = None
    source = None

    def append_to(self, events: list[TranscriptEvent]) -> None:
        events.append(self)

    def followed_by_assistant(self, text: AssistantTranscript) -> TranscriptEvent | None:
        return None

    def for_assigned_source(self, source, published: bool) -> tuple[TranscriptEvent, ...]:
        return (self,)

    def assistant_parts(self) -> tuple[str, ...]:
        return ()

    def with_publications(self, publications: tuple[MessageReference, ...]) -> TranscriptEvent:
        return self

    def for_external_source(self, source: TranscriptSource) -> tuple[TranscriptEvent, ...]:
        return (self,)

    def as_context(self) -> TranscriptEvent:
        """Non-text native evidence retains its own presentation declaration."""
        return self

    text_size: ClassVar[int] = 0
    native_inputs: ClassVar[frozenset[str]] = frozenset()
    incoming_sources: ClassVar[tuple[MessageReference, ...]] = ()

    def with_native_input(self, native_id: InputId | None) -> TranscriptEvent:
        return self

    @property
    @abstractmethod
    def routed(self) -> bool:
        return False

    def import_to(self, accept: Callable[[ImportedMessage], None]) -> None:
        """Non-conversation events produce no portable message."""

    def record_call(self, calls: dict[ToolCallId, OrderCall], since: float) -> None:
        """Only tool events change a unit's calls."""

    def edit_calls(self, thread: str, problem: str, since: float) -> tuple[tool_declaration.EditCall, ...]:
        return ()

    def before(self, at: float) -> bool:
        return self.timestamp < at

    def selected_input(self, selected: tuple[TranscriptEvent, ...]) -> tuple[TranscriptEvent, ...]:
        """Only the recorded input stands for the messages its wake selected."""
        return (self,)


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
    routing: Routing = NoTurnRouting()
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

    @property
    def routed(self) -> bool:
        return self.routing.outgoing


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
    @classmethod
    def of(cls, message: Message, *, reader: str) -> IncomingTranscript:
        return cls(message=message.envelope, reader=reader)

    @property
    def category(self) -> type[MessageCategory]:
        return self.message.role.category

    message: Message
    reader: str
    position: ClassVar[int]
    timestamp: ClassVar[float]

    def __post_init__(self) -> None:
        self.message.require_envelope()

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
    native_id: InputId | None = field(default=None, kw_only=True)

    def with_native_input(self, native_id: InputId | None) -> UserTranscript:
        return replace(self, native_id=native_id)

    @property
    def native_inputs(self) -> frozenset[str]:
        return frozenset((self.native_id,)) if self.native_id is not None else frozenset()

    @property
    def routed(self) -> bool:
        return self.routing.incoming

    def import_to(self, accept: Callable[[ImportedMessage], None]) -> None:
        ImportedRequest(self.text).import_to(accept)

    def selected_input(self, selected: tuple[TranscriptEvent, ...]) -> tuple[TranscriptEvent, ...]:
        return selected

    def for_assigned_source(self, source, published: bool) -> tuple[TranscriptEvent, ...]:
        if not self.routed:
            return (self,)
        requests = self.routing.requests
        originals = source.assignments(requests)
        remaining = tuple(reference for reference in requests if reference not in originals)
        if not remaining:
            return ()
        return (replace(self, routing=self.routing.with_requests(remaining)),)


class AssistantTranscript(AgentTextTranscript):
    def append_to(self, events: list[TranscriptEvent]) -> None:
        if events and (merged := events[-1].followed_by_assistant(self)) is not None:
            events[-1] = merged
        else:
            super().append_to(events)

    def followed_by_assistant(self, text: AssistantTranscript) -> TranscriptEvent:
        return replace(self, text=self.text + text.text)

    def for_assigned_source(self, source, published: bool) -> tuple[TranscriptEvent, ...]:
        return () if published else (self,)

    def assistant_parts(self) -> tuple[str, ...]:
        return (self.text,)

    def with_publications(self, publications: tuple[MessageReference, ...]) -> AssistantTranscript:
        return replace(self, routing=TurnRouting(publications=publications))

    def for_external_source(self, source: TranscriptSource) -> tuple[TranscriptEvent, ...]:
        references = self.routing.publications
        published = bool(references) and source.sent_messages(references).keys() >= set(references)
        return self.for_assigned_source(source, published)

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

    def result_parts(self, otherwise: tuple) -> tuple[SentTranscript, ...]:
        return (self,)

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
    tool_call_id: ToolCallId
    tool_name: str

    @property
    def title(self) -> str:
        return tool_declaration.ToolDeclaration.caption(self.tool_name, tool_declaration.NoToolRecord())

    @property
    def kind(self) -> type[tool_declaration.ToolAction]:
        return tool_declaration.ToolDeclaration.for_name(self.tool_name).declared_action()


@dataclass(frozen=True, kw_only=True)
class ToolStartTranscript(ToolTranscript):
    def edit_calls(self, thread: str, problem: str, since: float) -> tuple[tool_declaration.EditCall, ...]:
        if self.before(since):
            return ()
        return self.tool.edit_calls(
            self.tool_call_id, EventTime.capture(self.timestamp), thread, problem)

    raw_input: dict[str, JsonValue] = field(default_factory=dict)  # as the CLI sent it, for history
    tool: Annotated[
        tool_declaration.ToolDeclaration | tool_declaration.NoToolRecord,
        WireOmitDefault(),
        WireBind({"call_id": "tool_call_id"}),
    ] = tool_declaration.NoToolRecord()

    @property
    def title(self) -> str:
        return tool_declaration.ToolDeclaration.caption(self.tool_name, self.tool)

    def record_call(self, calls: dict[ToolCallId, OrderCall], since: float) -> None:
        """A call recorded before ``since`` is not work since then."""
        if not self.before(since):
            digest = self.tool.call_digest(self.tool_name, self.raw_input)
            calls[self.tool_call_id] = OrderCall(
                self.tool_call_id, self.tool_name, digest, RunningCallState)

    def import_to(self, accept: Callable[[ImportedMessage], None]) -> None:
        ImportedInput(f"[Historical tool call: {self.tool_name}]\n"
                      + json.dumps(self.raw_input, ensure_ascii=False)).import_to(accept)


@dataclass(frozen=True, kw_only=True)
class ToolEndTranscript(ToolTranscript):
    @staticmethod
    def parts_for(text, diff=NoToolDiff(), sent_message=NoSentMessage()):
        """The result owner selects its contents once, before transport or paint."""
        return sent_message.result_parts(diff.parts() + ((ToolText(text),) if text else ()))

    @property
    def result_parts(self):
        return self.parts_for(self.text, self.diff, self.sent_message)

    @property
    def text_size(self) -> int:
        return len(self.text)

    def record_call(self, calls: dict[ToolCallId, OrderCall], since: float) -> None:
        if self.tool_call_id in calls:
            calls[self.tool_call_id] = replace(
                calls[self.tool_call_id], state=SucceededCallState if self.ok else FailedCallState)

    def import_to(self, accept: Callable[[ImportedMessage], None]) -> None:
        outcome = "" if self.ok else ", failed"
        ImportedInput(f"[Historical tool result{outcome}]\n{self.text}").import_to(accept)

    text: str = ""
    ok: bool = True
    diff: Annotated[ToolDiff | NoToolDiff, WireOmitDefault()] = NoToolDiff()
    sent_message: Annotated[SentTranscript | NoSentMessage, WireOmitDefault()] = (
        NoSentMessage()
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
    tool_call_id: ToolCallId
    tool_name: str
    digest: str
    state: type[CallState]


@dataclass(frozen=True)
class TurnDigest:
    """One turn's events in their recorded order, read from the unit's own records."""

    turn_id: NativeTurnId
    events: tuple[TranscriptEvent, ...]

    def with_selected(self, selected: tuple[TranscriptEvent, ...]) -> TurnDigest:
        """The selected messages stand where the frame rendered around them was recorded; the
        stored frame stays the evidence of what the model saw."""
        return replace(self, events=tuple(
            kept for event in self.events for kept in event.selected_input(selected)))

    def before(self, at: float) -> bool:
        """The turn's last recorded event precedes ``at``; an empty turn is never before."""
        return bool(self.events) and self.events[-1].before(at)

    @property
    def start(self) -> EventTime:
        """The source-recorded first event, or an explicitly unrecorded start.

        Eventless external items store positions but no timestamp. Reading one must
        not invent a time from that position or from the current clock.
        """
        return EventTime.capture(self.events[0].timestamp) if self.events else UnrecordedEventTime()

    def starts_before(self, at: float) -> bool:
        return self.start.before(at)
