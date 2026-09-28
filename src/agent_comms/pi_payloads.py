"""Pi external records: normalize once, then use declaration-owned typed fields.

Unknown extension events/content and tool-defined arguments remain opaque. Known
records retain no raw wire mirror. FieldCodec is the sole primitive validator.
"""

from __future__ import annotations

import types
from abc import abstractmethod
from dataclasses import dataclass, field, fields, is_dataclass, replace
from typing import Any, ClassVar, Union, get_args, get_origin, get_type_hints

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec


def wire_field(name: str, default=None):
    return field(default=default, metadata={"wire_name": name, "wire_omit_default": True})


def normalize(target, value):
    """Project external extension fields using declarations before strict decoding."""
    origin, args = get_origin(target), get_args(target)
    if origin in (Union, types.UnionType):
        for option in args:
            if value is None and option is type(None):
                return None
            if isinstance(option, type) and isinstance(value, option):
                return value
            if isinstance(value, dict) and isinstance(option, type) and is_dataclass(option):
                return normalize(option, value)
            if isinstance(value, list) and get_origin(option) in (tuple, list):
                return normalize(option, value)
        return value  # FieldCodec rejects wrong primitive shapes.
    if origin in (list, tuple) and isinstance(value, list):
        return [normalize(args[0], item) for item in value]
    if isinstance(target, type) and issubclass(target, PiPayload):
        return target.normalize_wire(value)
    return value


class PiPayload:
    wire_tag: ClassVar[str | None] = None
    strict_fields: ClassVar[bool] = False

    @classmethod
    def normalize_wire(cls, value):
        if not isinstance(value, dict):
            raise ValueError(f"Expected {cls.__name__} object")
        member = cls
        data = {}
        if issubclass(cls, DeclaredFamily):
            member = cls.wire_member(value)
            if member.opaque:
                return {"kind": member.declared_name, "payload": value}
            data["kind"] = member.declared_name
        declared = [f for f in fields(member) if f.init]
        names = {f.metadata.get("wire_name", f.name) for f in declared}
        if member.strict_fields and set(value) - names - {member.wire_tag}:
            raise ValueError(f"Unexpected {member.__name__} fields")
        hints = get_type_hints(member)
        for f in declared:
            key = f.metadata.get("wire_name", f.name)
            if key in value:
                data[key] = member.normalize_field(hints[f.name], key, value[key], value)
        return data

    @classmethod
    def normalize_field(cls, target, key, value, record):
        return normalize(target, value)

    @classmethod
    def from_wire(cls, value):
        return FieldCodec.decode(cls, cls.normalize_wire(value))

    def to_wire(self):
        data = FieldCodec.encode(self)
        if isinstance(self, DeclaredFamily):
            name = data.pop("kind")
            if self.wire_tag is not None:
                data[self.wire_tag] = name
        return data


@dataclass(frozen=True)
class PiCost(PiPayload):
    input: float | None = wire_field("input")
    output: float | None = wire_field("output")
    cache_read: float | None = wire_field("cacheRead")
    cache_write: float | None = wire_field("cacheWrite")
    total: float | None = wire_field("total")


@dataclass(frozen=True)
class PiUsage(PiPayload):
    @classmethod
    def normalize_field(cls, target, key, value, record):
        if key == "totalTokens" and type(value) is not int:
            return None
        return super().normalize_field(target, key, value, record)

    input: int | None = wire_field("input")
    output: int | None = wire_field("output")
    cache_read: int | None = wire_field("cacheRead")
    cache_write: int | None = wire_field("cacheWrite")
    total_tokens: int | None = wire_field("totalTokens")
    cost: PiCost | None = wire_field("cost")
    reasoning: int | None = wire_field("reasoning")
    cache_write_1h: int | None = wire_field("cacheWrite1h")

    @property
    def positive_tokens(self) -> int | None:
        return (
            self.total_tokens if self.total_tokens is not None and self.total_tokens > 0 else None
        )


@dataclass(frozen=True)
class PiContent(PiPayload, DeclaredFamily, affix="Content"):
    def user_transcript(self):
        return []

    def assistant_transcript(self):
        return []

    wire_tag = "type"
    opaque: ClassVar[bool] = False
    text: ClassVar[str] = ""
    final_text_allowed: ClassVar[bool] = False
    tool_round_allowed: ClassVar[bool] = False

    @classmethod
    def wire_member(cls, value):
        try:
            return cls.decode(value.get(cls.wire_tag))
        except ValueError:
            return UnknownContent


@dataclass(frozen=True)
class TextContent(PiContent):
    def user_transcript(self):
        from .transcript_events import UserTranscript

        return [UserTranscript(self.text)]

    def assistant_transcript(self):
        from .transcript_events import AssistantTranscript

        return [AssistantTranscript(self.text)]

    text: str = field()
    text_signature: str | None = wire_field("textSignature")
    final_text_allowed = tool_round_allowed = True


@dataclass(frozen=True)
class ThinkingContent(PiContent):
    def assistant_transcript(self):
        from .transcript_events import ThinkingTranscript

        return [ThinkingTranscript(self.thinking)]

    thinking: str = ""
    thinking_signature: str | None = wire_field("thinkingSignature")
    final_text_allowed = tool_round_allowed = True


@dataclass(frozen=True)
class ToolCallContent(PiContent, declared_name="toolCall"):
    def assistant_transcript(self):
        from .transcript_events import ToolStartTranscript

        return [
            ToolStartTranscript(tool_call_id=self.id, tool_name=self.name, raw_input=self.arguments)
        ]

    id: str
    name: str
    arguments: dict[str, Any]
    tool_round_allowed = True


@dataclass(frozen=True)
class ImageContent(PiContent):
    def user_transcript(self):
        from .transcript_events import UserTranscript

        return [UserTranscript(f"[Image attachment: {self.mime_type or 'image'}]")]

    data: str
    mime_type: str = wire_field("mimeType")


@dataclass(frozen=True)
class UnknownContent(PiContent):
    payload: dict[str, Any]
    opaque = True


@dataclass(frozen=True)
class PiMessage(PiPayload, DeclaredFamily, affix="Message"):
    @property
    def parts(self) -> tuple[PiContent, ...]:
        return (TextContent(self.content),) if isinstance(self.content, str) else self.content or ()

    @property
    def unread_reply(self) -> bool:
        return False

    def transcript_events(self, context):
        return []

    wire_tag = "role"
    opaque: ClassVar[bool] = False
    assistant: ClassVar[bool] = False
    user: ClassVar[bool] = False
    content: tuple[PiContent, ...] | str | None = None
    usage: PiUsage | None = None
    input_id: str | None = wire_field("inputId")
    stop_reason: str | None = wire_field("stopReason")
    error_message: str | None = wire_field("errorMessage")

    @classmethod
    def wire_member(cls, value):
        try:
            return cls.decode(value.get(cls.wire_tag))
        except ValueError:
            return UnknownMessage

    @property
    def text(self) -> str:
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        return ("\n" if self.user else "").join(p.text for p in self.content if p.text)


class AssistantMessage(PiMessage):
    assistant = True

    @property
    def unread_reply(self) -> bool:
        from .transcript_events import AssistantTranscript, NoticeTranscript

        return any(
            isinstance(event, (AssistantTranscript, NoticeTranscript)) and event.text.strip()
            for event in self.transcript_events(None)
        )

    def transcript_events(self, context):
        from .transcript_events import AssistantTranscript, NoticeTranscript

        if (
            self.stop_reason in {"error", "aborted"}
            and self.error_message
            and self.error_message.strip()
        ):
            return [NoticeTranscript(f"[agent error] {self.error_message.strip()}")]
        events = []
        for part in self.parts:
            for event in part.assistant_transcript():
                if (
                    isinstance(event, AssistantTranscript)
                    and events
                    and isinstance(events[-1], AssistantTranscript)
                ):
                    events[-1] = replace(events[-1], text=events[-1].text + event.text)
                else:
                    events.append(event)
        return [replace(event, routing=context.routing if context else None) for event in events]


class UserMessage(PiMessage):
    user = True

    def transcript_events(self, context):
        from .routing import TurnRouting
        from .transcript_events import ContextTranscript, UserTranscript

        routing, display = context.routing, context.input_display
        # Preserve exact separators, including empty text blocks, for provenance.
        raw_text = "\n".join(part.text for part in self.parts if isinstance(part, TextContent))
        if display is not None and display.sent_text_digest is not None:
            if self.content is not None and display.matches(raw_text):
                routing = display.routing
            else:
                routing = display = None
        if routing is not None and routing.requests:
            return [
                UserTranscript(request.body, routing=TurnRouting((request,), None))
                for request in routing.requests
            ]
        parts = self.parts
        events = []
        if display is not None:
            if raw_text and raw_text != display.text:
                events.append(ContextTranscript(raw_text))
            if display.text is None:
                return events
            parts = (
                TextContent(display.text),
                *(part for part in parts if not isinstance(part, TextContent)),
            )
        for part in parts:
            events.extend(part.user_transcript())
        return [replace(event, routing=routing) for event in events]


@dataclass(frozen=True)
class ToolResultMessage(PiMessage, declared_name="toolResult"):
    tool_call_id: str = wire_field("toolCallId", "")
    tool_name: str = wire_field("toolName", "tool")
    is_error: bool = wire_field("isError", False)
    details: Any = None

    def transcript_events(self, context):
        from .routing import MessageRoute, TurnRouting
        from .tool_results import ToolDiff
        from .transcript_events import SentTranscript, ToolEndTranscript

        if not self.parts:
            return []
        output = "\n".join(part.text for part in self.parts if isinstance(part, TextContent))
        result = PiToolResult(content=self.parts, details=self.details)
        events = [
            ToolEndTranscript(
                text=output,
                tool_call_id=self.tool_call_id,
                tool_name=self.tool_name,
                ok=not self.is_error,
                diff=ToolDiff.from_result(self.tool_name, result, not self.is_error),
            )
        ]
        sent = (
            context.sent_tool_message(self.tool_name, output, not self.is_error)
            if context.sent_tool_message
            else None
        )
        if sent is not None:
            events.append(
                SentTranscript(
                    sent.body, routing=TurnRouting(reply=MessageRoute(sent.sender, (sent.target,)))
                )
            )
        return events


@dataclass(frozen=True, kw_only=True)
class UnknownMessage(PiMessage):
    payload: dict[str, Any]
    opaque = True


@dataclass(frozen=True)
class PiDelta(PiPayload, DeclaredFamily, affix="Delta"):
    wire_tag = "type"
    opaque: ClassVar[bool] = False
    progress: ClassVar[bool] = False

    @classmethod
    def wire_member(cls, value):
        try:
            return cls.decode(value.get(cls.wire_tag))
        except ValueError:
            return UnknownDelta

    @abstractmethod
    def emit(self, session): ...


@dataclass(frozen=True)
class TextDelta(PiDelta, declared_name="text_delta"):
    delta: str

    @property
    def progress(self):
        return bool(self.delta)

    def emit(self, session):
        session.output_started |= bool(self.delta)
        session.text_parts.append(self.delta)
        session.assistant_message_parts.append(self.delta)
        from .agent_events import Chunk

        return (Chunk(text=self.delta),)


@dataclass(frozen=True)
class ThinkingDelta(PiDelta, declared_name="thinking_delta"):
    delta: str

    @property
    def progress(self):
        return bool(self.delta)

    def emit(self, session):
        if not self.delta:
            return ()
        session.output_started = True
        from .agent_events import Thinking

        return (Thinking(text=self.delta),)


class ToolDelta:
    progress = True

    def emit(self, session):
        session.output_started = True
        return ()


class ToolcallStartDelta(ToolDelta, PiDelta, declared_name="toolcall_start"):
    pass


class ToolcallDelta(ToolDelta, PiDelta, declared_name="toolcall_delta"):
    pass


class ToolcallEndDelta(ToolDelta, PiDelta, declared_name="toolcall_end"):
    pass


@dataclass(frozen=True, kw_only=True)
class UnknownDelta(PiDelta):
    payload: dict[str, Any]
    opaque = True

    def emit(self, session):
        return ()


@dataclass(frozen=True)
class PiModel(PiPayload):
    provider: str | None = None
    id: str | None = None
    name: str | None = None
    context_window: int | None = wire_field("contextWindow")

    @property
    def display_name(self):
        name = self.id or self.name
        return f"{self.provider}/{name}" if self.provider and name else name or self.provider


@dataclass(frozen=True)
class PiContextUsage(PiPayload):
    @classmethod
    def normalize_field(cls, target, key, value, record):
        if key == "tokens" and type(value) is not int:
            return None
        return super().normalize_field(target, key, value, record)

    tokens: int | None = None
    context_window: int | None = wire_field("contextWindow")


@dataclass(frozen=True)
class PiResponseData(PiPayload, DeclaredFamily, affix="Data"):
    wire_tag = None
    opaque: ClassVar[bool] = False

    @classmethod
    def wire_member(cls, value):
        return cls if cls is not PiResponseData else cls.decode(value.get("kind"))


class EmptyData(PiResponseData):
    pass


@dataclass(frozen=True)
class StateData(PiResponseData):
    session_id: str | None = wire_field("sessionId")
    session_file: str | None = wire_field("sessionFile")
    session_name: str | None = wire_field("sessionName")
    native_input_proof_capability: str | None = wire_field("nativeInputProofCapability")
    model: PiModel | None = None
    thinking_level: str | None = wire_field("thinkingLevel")
    message_count: int | None = wire_field("messageCount")
    pending_message_count: int | None = wire_field("pendingMessageCount")
    is_streaming: bool | None = wire_field("isStreaming")
    is_compacting: bool | None = wire_field("isCompacting")


@dataclass(frozen=True)
class SessionStatsData(PiResponseData):
    session_id: str | None = wire_field("sessionId")
    session_file: str | None = wire_field("sessionFile")
    context_usage: PiContextUsage | None = wire_field("contextUsage")


@dataclass(frozen=True)
class ModelsData(PiResponseData):
    models: tuple[PiModel, ...] = ()


@dataclass(frozen=True)
class ThinkingLevelsData(PiResponseData):
    levels: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompactionData(PiResponseData):
    summary: str | None = None
    usage: PiUsage | None = None
    first_kept_entry_id: str | None = wire_field("firstKeptEntryId")
    tokens_before: int | None = wire_field("tokensBefore")
    estimated_tokens_after: int | None = wire_field("estimatedTokensAfter")


@dataclass(frozen=True)
class UnknownData(PiResponseData):
    payload: Any
    opaque = True

    @classmethod
    def normalize_wire(cls, value):
        return {"kind": cls.declared_name, "payload": value}


@dataclass(frozen=True)
class PiToolResult(PiPayload):
    content: tuple[PiContent, ...] = ()
    # Extension-defined details are deliberately opaque; no second tool schema.
    details: Any = None

    def text(self, limit=4000):
        text = "".join(part.text for part in self.content)
        return text[:limit] + ("…" if len(text) > limit else "")
