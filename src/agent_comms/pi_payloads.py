"""Pi external records: normalize once, then use declaration-owned typed fields.

Unknown extension events/content and tool-defined arguments remain opaque. Known
records retain no raw wire mirror. FieldCodec is the sole primitive validator.
"""

from __future__ import annotations

import re
import types
from abc import abstractmethod
from dataclasses import dataclass, field, fields, is_dataclass, replace
from functools import singledispatch
from typing import Annotated, Any, ClassVar, Literal, Union, get_args, get_origin

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, TextRepresentation
from .native_session_reopen import NativeSessionIdentity
from .native_file_artifact import NativeFileArtifact
from .pi_vocabulary import PiStopReason, ThinkingLevel, UnreportedStopReason


def wire_field(name: str, default=None):
    return field(default=default, metadata={"wire_name": name, "wire_omit_default": True})


def wire_options(target):
    """Python's annotation taxonomy is interpreted only at this ingress boundary."""
    return get_args(target) if get_origin(target) in (Union, types.UnionType) else (target,)


@singledispatch
def project_wire(value, target):
    """Project known Pi extension fields; FieldCodec still owns all decoding."""
    return value


@project_wire.register(dict)
def project_object(value, target):
    member = next(
        (
            option
            for option in wire_options(target)
            if isinstance(option, type) and is_dataclass(option)
        ),
        None,
    )
    if member is not None and issubclass(member, PiPayload):
        return member.normalize_wire(value)
    return value


@project_wire.register(list)
def project_array(value, target):
    member = next(
        (option for option in wire_options(target) if get_origin(option) in (tuple, list)),
        None,
    )
    return value if member is None else [project_wire(item, get_args(member)[0]) for item in value]


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
        hints = FieldCodec._types(member)
        for f in declared:
            key = f.metadata.get("wire_name", f.name)
            if key in value:
                data[key] = member.normalize_field(hints[f.name], key, value[key], value)
        return data

    @classmethod
    def normalize_field(cls, target, key, value, record):
        return project_wire(value, target)

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
    def tool_calls(self):
        return ()

    def preserve_evidence(self, raw: dict) -> PiContent:
        """Keep unrepresented native fields opaque, never equivalent to plain text."""
        return self if self.to_wire() == raw else UnknownContent(raw)

    def user_transcript(self):
        return []

    def assistant_transcript(self):
        return []

    wire_tag = "type"
    opaque: ClassVar[bool] = False
    text: ClassVar[str] = ""
    tool_round_allowed: ClassVar[bool] = False

    @property
    def final_text(self) -> str:
        from .native_pi import NativePiUnavailable

        raise NativePiUnavailable("Native Pi assistant returned non-text content")

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
    tool_round_allowed = True

    @property
    def final_text(self) -> str:
        return self.text


@dataclass(frozen=True)
class ThinkingContent(PiContent):
    def assistant_transcript(self):
        from .transcript_events import ThinkingTranscript

        return [ThinkingTranscript(self.thinking)]

    thinking: str = ""
    thinking_signature: str | None = wire_field("thinkingSignature")
    tool_round_allowed = True

    @property
    def final_text(self) -> str:
        return ""


@dataclass(frozen=True)
class ToolCallContent(PiContent, declared_name="toolCall"):
    def tool_calls(self):
        return (self,)

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

    def to_wire(self):
        return self.payload


class ProviderTransportStage(PiPayload, DeclaredFamily, affix="Stage"):
    """Native transport timing is display evidence, never input admission."""

    @classmethod
    def from_text(cls, value: str):
        try:
            member = cls.decode(value)
        except ValueError:
            return UnrecognizedTransportStage(value)
        return member()

    def to_text(self) -> str:
        return self.declared_name

    @property
    @abstractmethod
    def description(self) -> str: ...


@dataclass(frozen=True)
class BeforeMessageStreamStartStage(ProviderTransportStage):
    description = "before the provider response stream started"


@dataclass(frozen=True)
class AfterMessageStreamStartStage(ProviderTransportStage):
    description = "after the provider response stream started"


@dataclass(frozen=True)
class UnreportedTransportStage(ProviderTransportStage):
    description = "provider stage not reported"


@dataclass(frozen=True)
class UnrecognizedTransportStage(ProviderTransportStage):
    reported_phase: str

    def to_text(self) -> str:
        return self.reported_phase

    @property
    def description(self) -> str:
        return f"provider stage: {self.reported_phase}"


@dataclass(frozen=True)
class ProviderDiagnosticError(PiPayload):
    message: str
    name: str | None = None
    code: int | str | None = None

    @property
    def description(self) -> str:
        name = self.name or "Provider transport error"
        return f"{name} (code {self.code})" if self.code is not None else name


class TransportStageText(TextRepresentation):
    """The native provider's scalar spelling, not a second record decoder."""

    @classmethod
    def encode(cls, value: ProviderTransportStage) -> str:
        return value.to_text()

    @classmethod
    def from_text(cls, value: str) -> ProviderTransportStage:
        return ProviderTransportStage.from_text(value)


@dataclass(frozen=True)
class ProviderTransportDetails(PiPayload):
    configured_transport: str | None = wire_field("configuredTransport")
    fallback_transport: str | None = wire_field("fallbackTransport")
    events_emitted: bool | None = wire_field("eventsEmitted")
    phase: Annotated[ProviderTransportStage, TransportStageText] = field(
        default_factory=UnreportedTransportStage
    )
    request_bytes: int | None = wire_field("requestBytes")

    def __post_init__(self):
        if self.request_bytes is not None and self.request_bytes < 0:
            raise ValueError("Native request byte count must be nonnegative")

    @property
    def description(self) -> str:
        parts = [self.phase.description]
        if self.configured_transport is not None:
            parts.append(f"configured transport: {self.configured_transport}")
        if self.events_emitted is not None:
            parts.append("provider events emitted: " + ("yes" if self.events_emitted else "no"))
        if self.request_bytes is not None:
            parts.append(f"request: {self.request_bytes:,} bytes")
        if self.fallback_transport is not None:
            parts.append(f"reported fallback transport: {self.fallback_transport}")
        return "; ".join(parts)


@dataclass(frozen=True)
class PiDiagnostic(PiPayload, DeclaredFamily, affix="Diagnostic"):
    wire_tag = "type"
    opaque = False
    provider_connection_failure: ClassVar[bool] = False

    @classmethod
    def wire_member(cls, value):
        try:
            return cls.decode(value.get(cls.wire_tag))
        except ValueError:
            return UnknownDiagnostic

    @property
    def description(self) -> str:
        return ""


@dataclass(frozen=True)
class ProviderTransportFailureDiagnostic(PiDiagnostic):
    provider_connection_failure = True
    error: ProviderDiagnosticError
    details: ProviderTransportDetails
    timestamp: int | None = None

    @property
    def description(self) -> str:
        return "; ".join(
            part for part in (self.error.description, self.details.description) if part
        )


@dataclass(frozen=True)
class UnknownDiagnostic(PiDiagnostic):
    payload: dict[str, Any]
    opaque = True


@dataclass(frozen=True)
class PiMessage(PiPayload, DeclaredFamily, affix="Message"):
    def retained_tool_calls(self):
        return ()

    def retained_tool_facts(self, session, entry, originals):
        return ()

    def require_artifact_request(self, request):
        raise ValueError("Native message is not a completed file operation")

    def tracked_end(self, session) -> None:
        """Non-assistant messages cannot supply a tracked final response."""

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
    final_reply: ClassVar[bool] = False
    content: tuple[PiContent, ...] | str | None = None
    usage: PiUsage | None = None
    input_id: str | None = wire_field("inputId")
    input_digest: str | None = wire_field("inputDigest")
    stop_reason: type[PiStopReason] = wire_field("stopReason", UnreportedStopReason)
    error_message: str | None = wire_field("errorMessage")
    diagnostics: tuple[PiDiagnostic, ...] = ()

    def __post_init__(self):
        if isinstance(self.stop_reason, str):
            object.__setattr__(self, "stop_reason", PiStopReason.from_external(self.stop_reason))

    @classmethod
    def normalize_field(cls, target, key, value, record):
        if key == "stopReason":
            return PiStopReason.from_external(value).declared_name
        return super().normalize_field(target, key, value, record)

    @property
    def measured_tokens(self):
        return self.usage.positive_tokens if self.usage is not None else None

    def require_failed_terminal(self):
        raise ValueError("Native recovery requires an unambiguous failed terminal")

    async def apply_end(self, session):
        if False:
            yield

    async def apply_start(self, session, event):
        if False:
            yield

    def observe_start_abort(self, session, event):
        pass

    @property
    def invalidates_start(self):
        return self.user or self.assistant

    @property
    def update_context(self):
        return self.assistant

    @property
    def retry_progress(self):
        return False

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


class AbsentMessage(PiMessage):
    """An omitted external message carries no admission or output authority."""

    update_context = True


@dataclass(frozen=True)
class AssistantMessage(PiMessage):
    def retained_tool_calls(self):
        return tuple(call for part in self.parts for call in part.tool_calls())

    # Pi's assistant record always carries an array, including failed terminals.
    content: tuple[PiContent, ...] = field(default=(), metadata={"wire_required": True})
    # Original Pi completion metadata. These are observations, not registry
    # configuration or permission to select a model for a later request.
    api: str | None = wire_field("api")
    provider: str | None = wire_field("provider")
    model: str | None = wire_field("model")
    response_model: str | None = wire_field("responseModel")
    response_id: str | None = wire_field("responseId")
    provider_thinking_level: str | None = wire_field("providerThinkingLevel")
    assistant = True

    def tracked_end(self, session) -> None:
        self.stop_reason.tracked(session, self)

    @property
    def authoritative_text(self) -> str:
        return "".join(part.final_text for part in self.content)

    @property
    def final_reply(self):
        return self.stop_reason.successful

    async def apply_start(self, session, event):
        session.output.start_message()
        if False:
            yield

    @property
    def retry_progress(self):
        return self.stop_reason.permits_progress()

    def require_failed_terminal(self):
        self.stop_reason.require_failed_terminal(self)

    def require_failure_shape(self):
        if not self.error_message or self.content != ():
            raise ValueError("Native recovery requires an unambiguous failed terminal")

    async def apply_end(self, session):
        from .agent_events import CommittedProgress

        reason = self.stop_reason
        if reason.tool_round and session.committable_message(self.text):
            yield CommittedProgress(text=self.text)
        session.output.start_message()
        if self.usage is not None and session.accepts_output:
            yield session.usage.charge(self.usage)
        session.output.final_assistant_stop = reason.successful and session.started_input
        async for event in reason.apply(session, self):
            yield event

    @property
    def unread_reply(self) -> bool:
        from .transcript_events import AssistantTranscript, NoticeTranscript

        return any(
            isinstance(event, (AssistantTranscript, NoticeTranscript)) and event.text.strip()
            for event in self.transcript_events(None)
        )

    def transcript_events(self, context):
        from .transcript_events import AssistantTranscript, NoticeTranscript

        if self.stop_reason.failed and self.error_message and self.error_message.strip():
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

    def matches_input(self, text, native_id, require_id):
        return self.text == text and (not require_id or self.input_id == native_id)

    async def apply_start(self, session, event):
        async for update in session.admit_user_message(self, event):
            yield update

    def observe_start_abort(self, session, event):
        session.observe_input_during_abort(event)

    def transcript_events(self, context):
        from .transcript_events import ContextTranscript, UserTranscript

        routing, display = context.routing, context.input_display
        # Preserve exact separators, including empty text blocks, for provenance.
        raw_text = "\n".join(part.text for part in self.parts if isinstance(part, TextContent))
        if display is not None and display.sent_text_digest is not None:
            if self.content is not None and display.matches(raw_text):
                routing = display.routing
            else:
                routing = display = None
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
        return [replace(event, routing=routing).with_native_input(self.input_id) for event in events]


class ToolDetailsPayload(PiPayload):
    """The shared SDK details field owns normalization of its external shapes."""

    @classmethod
    def normalize_field(cls, target, key, value, record):
        if key == "details":
            return NativeToolDetails.normalize_wire(value)
        return super().normalize_field(target, key, value, record)


@dataclass(frozen=True)
class ToolResultMessage(ToolDetailsPayload, PiMessage, declared_name="toolResult"):
    tool_call_id: str = wire_field("toolCallId", "")
    tool_name: str = wire_field("toolName", "tool")
    is_error: bool = wire_field("isError", False)
    details: NativeToolDetails = field(default_factory=lambda: NoToolDetails())

    def completed_artifacts(self):
        from .native_tools import NativeTool

        return NativeTool.for_name(self.tool_name).result_artifacts(
            ProvidedToolResult(content=self.parts, details=self.details), not self.is_error)

    def require_tool_request(self, request):
        calls = tuple(call for call in request.retained_tool_calls()
                      if call.id == self.tool_call_id)
        if len(calls) != 1 or calls[0].name != self.tool_name:
            raise ValueError("Native tool result lacks its exact original SDK call")
        return calls[0]

    def require_artifact_request(self, request):
        self.require_tool_request(request)
        if not self.completed_artifacts():
            raise ValueError("Native result has no successful original file operation evidence")

    def retained_tool_facts(self, session, entry, originals):
        from .retained_task_facts import NativeArtifactTaskFact
        from .turn_context import JournalProvenance

        artifacts = self.completed_artifacts()
        if not artifacts:
            originals.pop(self.tool_call_id, None)
            return ()
        try:
            request = originals.pop(self.tool_call_id)
        except KeyError as error:
            raise ValueError("Completed file operation lacks its original SDK request") from error
        self.require_artifact_request(request)
        source = JournalProvenance(session.session_file,
            (request.require_entry_id(), entry.require_entry_id()))
        return tuple(NativeArtifactTaskFact(source, artifact) for artifact in artifacts)

    def transcript_events(self, context):
        from .native_tools import NativeTool
        from .transcript_events import ToolEndTranscript

        if not self.parts:
            return []
        output = "\n".join(part.text for part in self.parts if isinstance(part, TextContent))
        result = ProvidedToolResult(content=self.parts, details=self.details)
        events = [
            ToolEndTranscript(
                text=output,
                tool_call_id=self.tool_call_id,
                tool_name=self.tool_name,
                ok=not self.is_error,
                diff=NativeTool.for_name(self.tool_name).result_diff(result, not self.is_error),
            )
        ]
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
        session.output.append(self.delta)
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
        from .agent_events import Thinking

        return (Thinking(text=self.delta),)


class ToolDelta:
    progress = True

    def emit(self, session):
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
class PiModel(PiPayload, DeclaredFamily, affix="Model"):
    wire_tag = None
    opaque: ClassVar[bool] = False
    display_name: ClassVar[str | None] = None
    context_window: ClassVar[int | None] = None

    @classmethod
    def wire_member(cls, value):
        return ReportedModel

    @abstractmethod
    def matches_identity(self, selected: tuple[str, str]) -> bool: ...

    def require_selection(self, selected: str | None):
        raise ValueError("Prepared native model does not match the owner selection")

    def for_compaction(self, configured_model: str | None):
        return self.require_selection(configured_model)


class UnreportedModel(PiModel):
    """The external state has not reported a model; no identity is implied."""

    def matches_identity(self, selected: tuple[str, str]) -> bool:
        return False


@dataclass(frozen=True)
class ReportedModel(PiModel):
    provider: str | None = None
    id: str | None = None
    name: str | None = None
    context_window: int | None = wire_field("contextWindow")
    max_tokens: int | None = wire_field("maxTokens")

    @property
    def identity(self):
        return self.provider, self.id

    def matches_identity(self, selected: tuple[str, str]) -> bool:
        return self.identity == selected

    def require_selection(self, selected: str | None):
        if self.display_name != selected:
            return super().require_selection(selected)
        return self

    def for_compaction(self, configured_model: str | None):
        from .pi_summary_payloads import SelectedModel

        self.require_selection(configured_model)
        return SelectedModel(self.provider, self.id, self.context_window)

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

    @property
    def session_busy(self) -> bool:
        return False

    def require_request(self, request):
        raise ValueError("Native response data does not declare this selected request")

    def require_payload(self):
        return self

    def conflicts_attestation(self, attestation) -> bool:
        return False

    @classmethod
    def wire_member(cls, value):
        return cls if cls is not PiResponseData else cls.decode(value.get("kind"))


class EmptyData(PiResponseData):
    pass


class MissingData(PiResponseData):
    """An omitted/null native payload is not a successful data observation."""

    def require_payload(self):
        raise ValueError("Native response has no data")


@dataclass(frozen=True)
class SessionSwitchData(PiResponseData):
    """The SDK's actual session-replacement outcome, not a local readiness flag."""

    strict_fields = True
    cancelled: bool

    def require_switched(self) -> None:
        if self.cancelled:
            raise ValueError("Native saved-session replacement was cancelled")


@dataclass(frozen=True)
class NativeSessionSnapshot(PiPayload):
    """External snapshots may report only part of a saved session's identity."""

    session_id: str | None = wire_field("sessionId")
    session_file: str | None = wire_field("sessionFile")

    def _known_identity(self) -> dict[str, str]:
        return {
            component.name: value
            for component in fields(NativeSessionIdentity)
            if (value := getattr(self, component.name))
        }

    @property
    def identity(self) -> NativeSessionIdentity | None:
        known = self._known_identity()
        if len(known) != len(fields(NativeSessionIdentity)):
            return None
        return NativeSessionIdentity(**known)

    def conflicts(self, other: NativeSessionSnapshot) -> bool:
        known, observed = self._known_identity(), other._known_identity()
        return any(known[key] != observed[key] for key in known.keys() & observed.keys())

    def conflicts_attestation(self, attestation) -> bool:
        return attestation.conflicts(self)


@dataclass(frozen=True)
class StateData(NativeSessionSnapshot, PiResponseData):
    session_name: str | None = wire_field("sessionName")
    native_input_proof_capability: str | None = wire_field("nativeInputProofCapability")
    model: PiModel = field(default_factory=UnreportedModel)
    thinking_level: type[ThinkingLevel] | None = wire_field("thinkingLevel")
    message_count: int | None = wire_field("messageCount")
    pending_message_count: int | None = wire_field("pendingMessageCount")
    is_streaming: bool | None = wire_field("isStreaming")
    is_compacting: bool | None = wire_field("isCompacting")

    @classmethod
    def normalize_field(cls, target, key, value, record):
        if key == "model" and value is None:
            return {"kind": UnreportedModel.declared_name}
        return super().normalize_field(target, key, value, record)

    def matches_model(self, selected: tuple[str, str]) -> bool:
        return self.model.matches_identity(selected)

    @property
    def session_busy(self) -> bool:
        return self.is_streaming is True or self.is_compacting is True


@dataclass(frozen=True)
class SessionStatsData(NativeSessionSnapshot, PiResponseData):
    context_usage: PiContextUsage | None = wire_field("contextUsage")


@dataclass(frozen=True)
class ModelsData(PiResponseData):
    models: tuple[PiModel, ...] = ()


@dataclass(frozen=True)
class ThinkingLevelsData(PiResponseData):
    levels: tuple[type[ThinkingLevel], ...] = ()


@dataclass(frozen=True)
class CompactionData(PiResponseData):
    summary: str | None = None
    usage: PiUsage | None = None
    first_kept_entry_id: str | None = wire_field("firstKeptEntryId")
    tokens_before: int | None = wire_field("tokensBefore")
    estimated_tokens_after: int | None = wire_field("estimatedTokensAfter")

    @property
    def display_summary(self):
        from .backend import compaction_summary

        return compaction_summary(self.summary) if self.summary is not None else None

    def charge(self, session):
        yield from session.usage.charge_compaction(self.usage)


@dataclass(frozen=True)
class UnknownData(PiResponseData):
    payload: Any
    opaque = True

    @classmethod
    def normalize_wire(cls, value):
        return {"kind": cls.declared_name, "payload": value}


@dataclass(frozen=True)
class PiToolResult(PiPayload, DeclaredFamily, affix="ToolResult"):
    wire_tag = None
    opaque: ClassVar[bool] = False

    @classmethod
    def normalize_wire(cls, value):
        if value is None:
            return {"kind": MissingToolResult.declared_name}
        return super().normalize_wire(value)

    @classmethod
    def wire_member(cls, value):
        return ProvidedToolResult

    @abstractmethod
    def text(self, limit=4000): ...

    def edit_diff(self, ok):
        return None

    def artifacts(self, ok):
        return ()


class MissingToolResult(PiToolResult):
    """No native result was emitted; output and edit evidence are absent."""

    def text(self, limit=4000):
        return ""


@dataclass(frozen=True)
class ProvidedToolResult(ToolDetailsPayload, PiToolResult):
    content: tuple[PiContent, ...] = ()
    details: NativeToolDetails = field(default_factory=lambda: NoToolDetails())

    def text(self, limit=4000):
        text = "".join(part.text for part in self.content)
        return text[:limit] + ("…" if len(text) > limit else "")

    def edit_diff(self, ok):
        return self.details.edit_diff() if ok else None

    def artifacts(self, ok):
        return self.details.artifacts() if ok else ()


@dataclass(frozen=True)
class NativeEditDetails(PiPayload):
    """Native edit metadata, decoded only when projecting actual edit evidence."""

    patch: str | None = wire_field("patch")
    diff: str | None = wire_field("diff")
    first_changed_line: int | None = wire_field("firstChangedLine")

    @classmethod
    def capture_diff(cls, raw):
        try:
            details = cls.from_wire(raw)
        except (ValueError, TypeError):
            # Extension-defined metadata may be opaque. Preserve the original
            # result and output; unsupported formatting grants no diff evidence.
            return None
        return details.reported_diff()

    def reported_diff(self):
        from .tool_results import ToolDiff

        if self.patch and self.patch.strip():
            return ToolDiff(self.patch)
        if self.diff and self.diff.strip():
            # This is the external numbered edit format in retained Pi journals.
            return ToolDiff(self.diff, "numbered")
        return None


@dataclass(frozen=True)
class NativeToolDetails(PiPayload, DeclaredFamily, affix="ToolDetails"):
    """Decode the original owned result once; other extension details stay opaque."""
    opaque: ClassVar[bool] = False
    wire_tag = "agentCommsKind"

    @classmethod
    def normalize_wire(cls, value):
        if value is None:
            return {"kind": NoToolDetails.declared_name}
        if not isinstance(value, dict) or cls.wire_tag not in value:
            return {"kind": OpaqueToolDetails.declared_name, "payload": value}
        return super().normalize_wire(value)

    @classmethod
    def wire_member(cls, value):
        return cls.decode(value[cls.wire_tag])

    def edit_diff(self):
        return None

    def artifacts(self):
        return ()


class NoToolDetails(NativeToolDetails):
    pass


@dataclass(frozen=True)
class OpaqueToolDetails(NativeToolDetails):
    payload: Any
    opaque = True

    def edit_diff(self):
        return NativeEditDetails.capture_diff(self.payload)


@dataclass(frozen=True, kw_only=True)
class FileMutationToolDetails(NativeToolDetails, NativeEditDetails):
    """Known native publisher metadata, retaining the existing edit evidence."""
    strict_fields = True
    artifact: NativeFileArtifact = field(metadata={"wire_name": "agentCommsArtifact"})

    def edit_diff(self):
        return self.reported_diff()

    def artifacts(self):
        return (self.artifact,)


class McpCallPolicy(DeclaredFamily, affix="McpCallPolicy"):
    @classmethod
    def require_available(cls):
        pass

    @classmethod
    def require_unavailable(cls):
        raise ValueError("Inactive MCP servers cannot advertise calls")


class AutomaticMcpCallPolicy(McpCallPolicy):
    pass


class ConfirmMcpCallPolicy(McpCallPolicy):
    pass


class UnavailableMcpCallPolicy(McpCallPolicy):
    @classmethod
    def require_available(cls):
        raise ValueError("Ready MCP servers must advertise their call policy")

    @classmethod
    def require_unavailable(cls):
        pass


class McpServerState(DeclaredFamily, affix="McpServerState"):
    @classmethod
    @abstractmethod
    def validate(cls, calls: type[McpCallPolicy], counts: tuple[int, ...]): ...


class ReadyMcpServerState(McpServerState):
    @classmethod
    def validate(cls, calls, counts):
        calls.require_available()


class ErrorMcpServerState(McpServerState):
    @classmethod
    def validate(cls, calls, counts):
        calls.require_unavailable()
        if any(counts):
            raise ValueError("Inactive MCP server counts must be zero")


class DisabledMcpServerState(ErrorMcpServerState):
    pass


class TrustRequiredMcpServerState(ErrorMcpServerState):
    pass


class UnsupportedEnvMcpServerState(ErrorMcpServerState):
    pass


class DeniedMcpServerState(ErrorMcpServerState):
    pass


class StaleRestartRequiredMcpServerState(ErrorMcpServerState):
    pass


class ConnectingMcpServerState(ErrorMcpServerState):
    pass


class ApprovedMcpServerState(ErrorMcpServerState):
    pass


@dataclass(frozen=True)
class McpServerReceipt:
    id: str
    scope: Literal["user", "project"]
    state: type[McpServerState]
    calls: type[McpCallPolicy]
    tools: int
    resources: int
    prompts: int

    def __post_init__(self):
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", self.id) is None:
            raise ValueError("Invalid external MCP server identity")
        counts = self.tools, self.resources, self.prompts
        if any(not 0 <= count <= 10_000 for count in counts):
            raise ValueError("Invalid external MCP server counts")
        self.state.validate(self.calls, counts)


@dataclass(frozen=True)
class McpLiveReceipt:
    version: Literal[1]
    source: Literal["pi-mcp-client"]
    input_id: str = field(metadata={"wire_name": "inputId"})
    state: Literal["running"]
    lifetime: Literal["turn"]
    servers: tuple[McpServerReceipt, ...]

    @classmethod
    def from_status(cls, text: str | None, input_id: str) -> McpLiveReceipt | None:
        """Decode the package's external status claim once at its boundary.

        Same-user extensions can imitate this claim. It is never a package
        attestation or permission, even when the current input identity matches.
        """
        import json
        from .pi_rpc import unique_fields

        if text is None or len(text) > 8192:
            return None
        try:
            receipt = FieldCodec.decode(cls, json.loads(text, object_pairs_hook=unique_fields))
        except (TypeError, ValueError):
            return None
        return receipt if receipt.input_id == input_id else None

    def __post_init__(self):
        if re.fullmatch(r"[a-f0-9]{32}", self.input_id) is None:
            raise ValueError("Invalid external MCP input identity")
        if len(self.servers) > 32 or len({server.id for server in self.servers}) != len(
            self.servers
        ):
            raise ValueError("Invalid external MCP server collection")
