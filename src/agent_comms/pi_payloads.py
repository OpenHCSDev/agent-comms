"""Pi external records: normalize once, then use declaration-owned typed fields.

Unknown extension events/content and tool-defined arguments remain opaque. Known
records retain no raw wire mirror. FieldCodec is the sole primitive validator.
"""

from __future__ import annotations

import math
import re
import types
from abc import abstractmethod
from decimal import Decimal
from dataclasses import dataclass, field, fields, is_dataclass, replace
from functools import singledispatch
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, Union, get_args, get_origin

from .declared_family import DeclaredFamily, UnknownFamilyMember
from .field_codec import FieldCodec, Id128Text, MalformedRecord, TextRepresentation
from .native_file_artifact import NativeFileArtifact
from .turn_failure import ProviderTransportFailed
from .native_session_reopen import NativeSessionIdentity
from .pi_counts import NonnegativePiCount, PiCount, UnreportedCount
from .pi_vocabulary import (
    NoThinkingOverride,
    PiStopReason,
    ThinkingLevel,
    ThinkingLevelField,
    UnreportedStopReason,
)
from .tool_declaration import ToolDeclaration
from .transcript_events import SentTranscript
from .send_result import CorrectionResult, CorrectionUnreported

if TYPE_CHECKING:
    from .agent_events import ProviderUsage
    from .turn_phase import TurnPhase
    from .turn_usage import UsageAccount


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
    opaque: ClassVar[bool] = False
    strict_fields: ClassVar[bool] = False

    @classmethod
    def normalize_wire(cls, value):
        if not isinstance(value, dict):
            raise MalformedRecord(f"Expected {cls.__name__} object")
        member = cls
        data = {}
        if issubclass(cls, DeclaredFamily):
            member = cls.wire_member(value)
            data["kind"] = member.declared_name
        declared = FieldCodec.record_fields(member)
        names = {key for _, key in declared}
        if member.strict_fields and set(value) - names - {member.wire_tag}:
            raise MalformedRecord(f"Unexpected {member.__name__} fields")
        hints = FieldCodec._types(member)
        for f, key in declared:
            # Native null means omitted where the declaration names its not-reported member.
            if key in value and not (value[key] is None and isinstance(f.default, PiPayload)):
                data[key] = member.normalize_field(hints[f.name], key, value[key], value)
        if member.opaque:
            # An opaque member keeps the whole record beside the fields it declares.
            data.update(payload=value)
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
            data.pop("kind")
            if self.wire_tag is not None:
                data[self.wire_tag] = self.wire_discriminator()
        return data

    def wire_discriminator(self):
        """Refined internal members borrow their original external declaration."""
        return FieldCodec.encode(type(self))


@dataclass(frozen=True)
class ProviderRetryOutcome(PiPayload, DeclaredFamily, affix="ProviderRetry"):
    @classmethod
    def wire_member(cls, value):
        try:
            success = FieldCodec.decode(bool, value.get("success"))
        except MalformedRecord as error:
            raise MalformedRecord(
                "Provider retry end requires a boolean success outcome"
            ) from error
        if success:
            return SucceededProviderRetry
        final_error = value.get("finalError")
        if not isinstance(final_error, str) or not final_error.strip():
            raise MalformedRecord("Failed provider retry end requires finalError")
        return FailedProviderRetry

    @abstractmethod
    def result(self, attempt: int, redact): ...

    def apply(self, event, session):
        self.finish_request(session)
        return ()

    @abstractmethod
    def recovery_events(self, session): ...

    @abstractmethod
    def finish_request(self, session) -> None: ...

    @abstractmethod
    def retry_wire(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class SucceededProviderRetry(ProviderRetryOutcome, declared_name="succeeded"):
    def result(self, attempt: int, redact):
        from .acp_failure import ProviderRetryResult

        return ProviderRetryResult(success=True, attempt=attempt)

    def recovery_events(self, session):
        from .turn_phase import ModelWaitPhase

        return (session.watchdog.state(
            "recovered", "provider_auto_retry_progress", 0, phase=ModelWaitPhase()
        ),)

    def finish_request(self, session) -> None:
        session.request_retried()

    def retry_wire(self) -> dict[str, object]:
        return {"success": True}


@dataclass(frozen=True)
class FailedProviderRetry(ProviderRetryOutcome, declared_name="failed"):
    final_error: str = field(metadata={"wire_name": "finalError"})

    def __post_init__(self) -> None:
        if not self.final_error.strip():
            raise ValueError("Failed provider retry end requires finalError")

    def result(self, attempt: int, redact):
        from .acp_failure import ProviderRetryResult

        return ProviderRetryResult(
            success=False, attempt=attempt, final_error=redact(self.final_error)
        )

    def apply(self, event, session):
        from .turn_phase import ModelWaitPhase

        super().apply(event, session)
        session.watchdog.retry_recovery_pending = False
        return (
            *event.exhausted(session),
            session.watchdog.state(
                "failed", "provider_retry_exhausted", 0, phase=ModelWaitPhase()
            ),
        )

    def recovery_events(self, session):
        return ()

    def finish_request(self, session) -> None:
        from .turn_failure import ModelRequestFailed

        session.output.record_failure(ModelRequestFailed(self.final_error))

    def retry_wire(self) -> dict[str, object]:
        return {"success": False, "finalError": self.final_error}


@dataclass(frozen=True)
class PiCost(PiPayload):
    input: float = wire_field("input", 0.0)
    output: float = wire_field("output", 0.0)
    cache_read: float = wire_field("cacheRead", 0.0)
    cache_write: float = wire_field("cacheWrite", 0.0)
    total: float = wire_field("total", 0.0)

    @classmethod
    def normalize_field(cls, target: object, key: str, value: object, record: object) -> object:
        amount = super().normalize_field(target, key, value, record)
        if type(amount) in (int, float) and not (math.isfinite(amount) and amount >= 0):
            raise MalformedRecord(f"Pi cost {key} must be a finite nonnegative number.")
        return amount

    def charged(self) -> Decimal:
        return Decimal(str(self.total))




@dataclass(frozen=True)
class PiUsage(PiPayload):
    @classmethod
    def normalize_field(cls, target, key, value, record):
        if key == "totalTokens" and type(value) is not int:
            return None
        return super().normalize_field(target, key, value, record)

    input: NonnegativePiCount = wire_field("input", UnreportedCount())
    output: NonnegativePiCount = wire_field("output", UnreportedCount())
    cache_read: NonnegativePiCount = wire_field("cacheRead", UnreportedCount())
    cache_write: NonnegativePiCount = wire_field("cacheWrite", UnreportedCount())
    total_tokens: PiCount = wire_field("totalTokens", UnreportedCount())
    cost: PiCost = wire_field("cost", PiCost())
    reasoning: NonnegativePiCount = wire_field("reasoning", UnreportedCount())
    cache_write_1h: NonnegativePiCount = wire_field("cacheWrite1h", UnreportedCount())

    def charges(self, account: UsageAccount) -> tuple[ProviderUsage, ...]:
        return (account.charge(self),)


@dataclass(frozen=True)
class UnreportedUsage(PiUsage):
    """Pi sent no usage object: nothing is charged and no token total is known."""

    def charges(self, account: UsageAccount) -> tuple[ProviderUsage, ...]:
        return ()


@dataclass(frozen=True)
class PiContent(PiPayload, DeclaredFamily, affix="Content"):
    def tool_calls(self) -> tuple[ToolCallContent, ...]:
        return ()

    def preserve_evidence(self, raw: dict) -> PiContent:
        """Keep unrepresented native fields opaque, never equivalent to plain text."""
        return self if self.to_wire() == raw else UnknownContent(raw)

    def user_transcript(self, position: int, timestamp: float):
        return []

    def assistant_transcript(self, position: int, timestamp: float):
        return []

    wire_tag = "type"
    text: ClassVar[str] = ""
    tool_round_allowed: ClassVar[bool] = False

    @property
    def final_text(self) -> str:
        from .native_pi import NativePiUnavailable

        raise NativePiUnavailable("Native Pi assistant returned non-text content")

    @classmethod
    def wire_member(cls, value):
        try:
            return cls.member_named(value.get(cls.wire_tag))
        except UnknownFamilyMember:
            return UnknownContent


@dataclass(frozen=True)
class TextContent(PiContent):
    def user_transcript(self, position: int, timestamp: float):
        from .transcript_events import UserTranscript

        return [UserTranscript(self.text, position=position, timestamp=timestamp)]

    def assistant_transcript(self, position: int, timestamp: float):
        from .transcript_events import AssistantTranscript

        return [AssistantTranscript(self.text, position=position, timestamp=timestamp)]

    text: str = field()
    text_signature: str | None = wire_field("textSignature")
    tool_round_allowed = True

    @property
    def final_text(self) -> str:
        return self.text


@dataclass(frozen=True)
class ThinkingContent(PiContent):
    def assistant_transcript(self, position: int, timestamp: float):
        from .transcript_events import ThinkingTranscript

        return [ThinkingTranscript(self.thinking, position=position, timestamp=timestamp)]

    thinking: str = ""
    thinking_signature: str | None = wire_field("thinkingSignature")
    tool_round_allowed = True

    @property
    def final_text(self) -> str:
        return ""


@dataclass(frozen=True)
class ToolCallContent(PiContent, declared_name="toolCall"):
    def tool_calls(self) -> tuple[ToolCallContent, ...]:
        return (self,)

    def assistant_transcript(self, position: int, timestamp: float):
        from .transcript_events import ToolStartTranscript

        return [
            ToolStartTranscript(position=position, timestamp=timestamp, tool_call_id=self.id, tool_name=self.name, raw_input=self.arguments,
                                tool=ToolDeclaration.record(self.name, self.arguments, call_id=self.id))
        ]

    id: str
    name: str
    arguments: object
    tool_round_allowed = True


@dataclass(frozen=True)
class ImageContent(PiContent):
    def user_transcript(self, position: int, timestamp: float):
        from .transcript_events import UserTranscript

        return [UserTranscript(f"[Image attachment: {self.mime_type or 'image'}]", position=position, timestamp=timestamp)]

    data: str
    mime_type: str = wire_field("mimeType")


@dataclass(frozen=True)
class UnknownContent(PiContent):
    payload: dict[str, Any]
    opaque = True

    def to_wire(self):
        return self.payload


class ProviderTransportStage(PiPayload, DeclaredFamily, affix="Stage"):
    """Show native transport timings without permitting input."""

    @classmethod
    def from_text(cls, value: str):
        try:
            member = cls.member_named(value)
        except UnknownFamilyMember:
            return UnrecognizedTransportStage(value)
        return member()

    def to_text(self) -> str:
        return FieldCodec.encode(type(self))

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
class ProviderDiagnosticError(PiPayload, DeclaredFamily, affix="ProviderDiagnosticError"):
    message: str
    name: str | None = None

    @classmethod
    def wire_member(cls, value: dict[str, object]) -> type[ProviderDiagnosticError]:
        return CodedProviderDiagnosticError if "code" in value else CodelessProviderDiagnosticError

    @property
    def description(self) -> str:
        return self.name or "Provider transport error"

@dataclass(frozen=True)
class CodelessProviderDiagnosticError(ProviderDiagnosticError):
    """Pi reported a provider error without a code."""

@dataclass(frozen=True, kw_only=True)
class CodedProviderDiagnosticError(ProviderDiagnosticError):
    code: int | str

    @property
    def description(self) -> str:
        return f"{super().description} (code {self.code})"


class TransportStageText(TextRepresentation[ProviderTransportStage]):
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
    provider_connection_failure: ClassVar[bool] = False

    @classmethod
    def wire_member(cls, value):
        try:
            return cls.member_named(value.get(cls.wire_tag))
        except UnknownFamilyMember:
            return UnknownDiagnostic

    def refine_model_request_failure(self, failure):
        return failure

    def turn_phase(self, phase: TurnPhase) -> TurnPhase:
        return phase

    @property
    def description(self) -> str:
        return ""


@dataclass(frozen=True)
class ProviderTransportFailureDiagnostic(PiDiagnostic):
    provider_connection_failure = True
    error: ProviderDiagnosticError
    details: ProviderTransportDetails
    timestamp: int | None = None

    def turn_phase(self, phase: TurnPhase) -> TurnPhase:
        from .turn_phase import FailedPhase
        return FailedPhase(error=self.error)

    def refine_model_request_failure(self, failure):
        return ProviderTransportFailed(failure.text)

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
    def annotation_disclosure(self):
        from .working_memory_disclosure import WithheldDisclosure

        return WithheldDisclosure()

    @property
    def parts(self) -> tuple[PiContent, ...]:
        return (TextContent(self.content),) if isinstance(self.content, str) else self.content or ()

    @property
    def tool_calls(self) -> tuple[ToolCallContent, ...]:
        """The tool calls this message makes, in the order it makes them."""
        return tuple(call for part in self.parts for call in part.tool_calls())

    def storage_refusals(self) -> tuple[tuple[str, StorageDenialToolDetails], ...]:
        return ()

    @property
    def unread_reply(self) -> bool:
        return False

    def transcript_events(self, context, timestamp: float):
        return []

    wire_tag = "role"
    assistant: ClassVar[bool] = False
    user: ClassVar[bool] = False
    final_reply: ClassVar[bool] = False
    content: tuple[PiContent, ...] | str | None = None
    usage: PiUsage = wire_field("usage", UnreportedUsage())
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
            return FieldCodec.encode(PiStopReason.from_external(value))
        return super().normalize_field(target, key, value, record)

    @property
    def measured_tokens(self) -> PiCount:
        return self.usage.total_tokens

    def require_failed_terminal(self):
        raise ValueError("Native recovery requires an unambiguous failed terminal")

    async def apply_end(self, session):
        return ()

    async def apply_start(self, session, event):
        return ()

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
            return cls.member_named(value.get(cls.wire_tag))
        except UnknownFamilyMember:
            return UnknownMessage

    @property
    def text(self) -> str:
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        return ("\n" if self.user else "").join(p.text for p in self.content if p.text)


class AbsentMessage(PiMessage):
    """An omitted external message permits neither new input nor output."""

    update_context = True


@dataclass(frozen=True)
class AssistantMessage(PiMessage):
    def annotation_disclosure(self):
        from .working_memory_disclosure import PublicInstructionDisclosure

        return PublicInstructionDisclosure()

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

    @property
    def authoritative_text(self) -> str:
        return "".join(part.final_text for part in self.content)

    @property
    def final_reply(self):
        return self.stop_reason.successful

    async def apply_start(self, session, event):
        await session.assistant_started()
        return ()

    @property
    def retry_progress(self):
        return self.stop_reason.permits_progress()

    def require_failed_terminal(self):
        self.stop_reason.require_failed_terminal()

    async def apply_end(self, session):
        from .agent_events import CommittedProgress

        reason = self.stop_reason
        updates = []
        if reason.tool_round and session.committable_message(self.text):
            updates.append(CommittedProgress(text=self.text))
        session.output.start_message()
        if session.accepts_output:
            updates.extend(self.usage.charges(session.usage))
        session.output.final_assistant_stop = reason.successful and session.started_input
        updates.extend([event async for event in reason.apply(session, self)])
        await session.assistant_ended()
        return tuple(updates)

    @property
    def error_notice(self) -> str:
        return self.stop_reason.error_notice(self)

    @property
    def unread_reply(self) -> bool:
        return bool(self.text.strip() or self.error_notice)

    def transcript_events(self, context, timestamp: float):
        from .transcript_events import AssistantTranscript, NoticeTranscript

        if notice := self.error_notice:
            return [NoticeTranscript(f"[agent error] {notice}", position=context.position, timestamp=timestamp)]
        events = []
        for part in self.parts:
            for event in part.assistant_transcript(context.position, timestamp):
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

    def matches_input(self, text, native_id):
        return self.text == text and self.input_id == native_id

    async def apply_start(self, session, event):
        return await session.accept_user_message(self, event)

    def observe_start_abort(self, session, event):
        session.observe_input_during_abort(event)

    def transcript_events(self, context, timestamp: float):
        from .transcript_events import ContextTranscript

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
                events.append(ContextTranscript(raw_text, position=context.position, timestamp=timestamp))
            if display.text is None:
                return events
            parts = (
                TextContent(display.text),
                *(part for part in parts if not isinstance(part, TextContent)),
            )
        for part in parts:
            events.extend(part.user_transcript(context.position, timestamp))
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

    def storage_refusals(self) -> tuple[tuple[str, StorageDenialToolDetails], ...]:
        if not self.is_error:
            return ()
        return tuple((self.tool_call_id, denial) for denial in self.details.storage_refusals())

    def transcript_events(self, context, timestamp: float):
        from .tool_declaration import ToolDeclaration
        from .transcript_events import ToolEndTranscript

        if not self.parts:
            return []
        output = "\n".join(part.text for part in self.parts if isinstance(part, TextContent))
        result = ProvidedToolResult(content=self.parts, details=self.details)
        events = [
            ToolEndTranscript(
                position=context.position, timestamp=timestamp,
                text=output,
                tool_call_id=self.tool_call_id,
                tool_name=self.tool_name,
                ok=not self.is_error,
                diff=ToolDeclaration.for_name(self.tool_name).result_diff(result, not self.is_error),
                sent_message=result.sent_message(not self.is_error),
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
    progress: ClassVar[bool] = False
    response_progress: ClassVar[bool] = False

    @classmethod
    def wire_member(cls, value):
        try:
            return cls.member_named(value.get(cls.wire_tag))
        except UnknownFamilyMember:
            return UnknownDelta

    def events(self):
        return ()

    async def emit(self, session):
        return self.events()


@dataclass(frozen=True)
class TextDelta(PiDelta, declared_name="text_delta"):
    delta: str
    response_progress = True

    @property
    def progress(self):
        return bool(self.delta)

    def events(self):
        return (events.Chunk(text=self.delta),)

    async def emit(self, session):
        await session.text_received(self.delta)
        return self.events()


@dataclass(frozen=True)
class ThinkingDelta(PiDelta, declared_name="thinking_delta"):
    delta: str

    @property
    def progress(self):
        return bool(self.delta)

    def events(self):
        return (events.Thinking(text=self.delta),) if self.delta else ()


class ToolDelta:
    progress = True
    response_progress = True

    async def emit(self, session):
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

    async def emit(self, session):
        return ()


@dataclass(frozen=True)
class PiModel(PiPayload, DeclaredFamily, affix="Model"):
    wire_tag = None
    display_name: ClassVar[str | None] = None
    context_window: ClassVar[PiCount] = UnreportedCount()

    @classmethod
    def wire_member(cls, value):
        return ReportedModel

    @abstractmethod
    def matches_identity(self, selected: tuple[str, str]) -> bool: ...

    def require_selection(self, selected: str | None):
        raise ValueError("Prepared native model does not match the owner selection")


class UnreportedModel(PiModel):
    """The external state has not reported a model; no identity is implied."""

    def matches_identity(self, selected: tuple[str, str]) -> bool:
        return False


@dataclass(frozen=True)
class ReportedModel(PiModel):
    provider: str | None = None
    id: str | None = None
    name: str | None = None
    context_window: PiCount = wire_field("contextWindow", UnreportedCount())
    max_tokens: PiCount = wire_field("maxTokens", UnreportedCount())

    @property
    def identity(self):
        return self.provider, self.id

    def matches_identity(self, selected: tuple[str, str]) -> bool:
        return self.identity == selected

    def require_selection(self, selected: str | None):
        if self.display_name != selected:
            return super().require_selection(selected)
        return self

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

    tokens: PiCount = wire_field("tokens", UnreportedCount())
    context_window: PiCount = wire_field("contextWindow", UnreportedCount())

    @property
    def token_total(self) -> PiCount:
        return self.tokens


@dataclass(frozen=True)
class PiResponseData(PiPayload, DeclaredFamily, affix="Data"):
    wire_tag = None

    @property
    def session_busy(self) -> bool:
        return False

    def require_payload(self):
        return self

    def conflicts_attestation(self, attestation) -> bool:
        return False

    @classmethod
    def wire_member(cls, value):
        return cls if cls is not PiResponseData else cls.member_named(value.get("kind"))


@dataclass(frozen=True)
class QueueClearedData(PiResponseData):
    """Native IDs actually removed, never the texts of possible candidates."""

    input_ids: tuple[str, ...] = field(metadata={"wire_name": "inputIds"})

    def require_requested(self, requested: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(self.input_ids)) != len(self.input_ids) or not set(self.input_ids) <= set(requested):
            raise ValueError("Native clear returned duplicate or unrequested input IDs")
        return self.input_ids


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
    native_input_proof_capability: str | None = wire_field("nativeInputProofCapability")
    # Native omission and null both mean no model observation. Omit the named
    # default on output; its internal family tag is not a native model object.
    model: PiModel = field(default_factory=UnreportedModel, metadata={"wire_omit_default": True})
    thinking_level: ThinkingLevelField = wire_field("thinkingLevel", default=NoThinkingOverride)
    message_count: NonnegativePiCount = wire_field("messageCount", UnreportedCount())
    pending_message_count: NonnegativePiCount = wire_field("pendingMessageCount", UnreportedCount())
    is_streaming: bool | None = wire_field("isStreaming")
    is_compacting: bool | None = wire_field("isCompacting")

    @classmethod
    def normalize_field(cls, target, key, value, record):
        if key == "model" and value is None:
            return {"kind": UnreportedModel.declared_name}
        return super().normalize_field(target, key, value, record)

    def agent_info(self, session):
        """Project this actual native state, not a later usage-only observation."""
        from .agent_events import AgentInfo

        return AgentInfo(
            model=self.model.display_name,
            thinking_level=self.thinking_level,
            session_file=session.active_session_file,
            context_size=self.model.context_window,
        )

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
    usage: PiUsage = wire_field("usage", UnreportedUsage())
    first_kept_entry_id: str | None = wire_field("firstKeptEntryId")
    tokens_before: PiCount = wire_field("tokensBefore", UnreportedCount())
    estimated_tokens_after: PiCount = wire_field("estimatedTokensAfter", UnreportedCount())

    @property
    def display_summary(self):
        from .backend import compaction_summary

        return compaction_summary(self.summary) if self.summary is not None else None

    def charge(self, session):
        yield from self.usage.charges(session.usage)


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

    def sent_message(self, ok):
        return None


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

    def sent_message(self, ok):
        return self.details.sent_message() if ok else None


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
        except MalformedRecord:
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
        return cls.member_named(value[cls.wire_tag])

    def edit_diff(self):
        return None

    def sent_message(self):
        return None

    def storage_refusals(self) -> tuple[StorageDenialToolDetails, ...]:
        return ()


@dataclass(frozen=True)
class StorageDenialToolDetails(NativeToolDetails):
    """The owner preflight's refusal, carried in the actual native tool result."""

    reason: str

    def storage_refusals(self) -> tuple[StorageDenialToolDetails, ...]:
        return (self,)


@dataclass(frozen=True, kw_only=True)
class PublishedMessageToolDetails(NativeToolDetails):
    """The send owner supplies its original committed message, not a guessed acknowledgement."""

    strict_fields = True
    id: str
    message: SentTranscript
    correction: CorrectionResult = field(default_factory=CorrectionUnreported)

    def __post_init__(self):
        if self.id != self.message.source.message_id:
            raise ValueError("Published tool result belongs to another message")

    def sent_message(self):
        return self.message


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
class McpLiveAcknowledgement:
    version: Literal[1]
    source: Literal["pi-mcp-client"]
    input_id: str = field(metadata={"wire_name": "inputId"})
    state: Literal["running"]
    lifetime: Literal["turn"]
    servers: tuple[McpServerReceipt, ...]

    @classmethod
    def from_status(cls, text: str | None, input_id: str) -> McpLiveAcknowledgement | None:
        """Decode the package's external status claim once at its boundary.

        Same-user extensions can imitate this claim. It is never a package
        attestation or permission, even when the current input identity matches.
        """
        if text is None or len(text) > 8192:
            return None
        try:
            acknowledgement = FieldCodec.decode(cls, FieldCodec.parse(text))
        except MalformedRecord:
            return None
        return acknowledgement if acknowledgement.input_id == input_id else None

    def __post_init__(self):
        Id128Text.from_text(self.input_id)
        if len(self.servers) > 32 or len({server.id for server in self.servers}) != len(
            self.servers
        ):
            raise ValueError("Invalid external MCP server collection")


# Core observations refer to the payload types above; conversion methods use
# this module only after those declarations are complete.
from . import agent_events as events  # noqa: E402
