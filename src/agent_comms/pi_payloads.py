"""Pi external records: normalize once, then use declaration-owned typed fields.

Unknown extension events/content and tool-defined arguments remain opaque. Known
records retain no raw wire mirror. FieldCodec is the sole primitive validator.
"""

from __future__ import annotations

import typing
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from pydantic import JsonValue

from agent_comms import agent_events as events
from agent_comms.execution.acp_failure import ExhaustedRetry, FailureSource, RecoveredRetry
from agent_comms.execution.annotation_disclosure import (
    PublicInstructionAnnotations,
    WithheldAnnotations,
)
from agent_comms.execution.native_file_artifact import NativeFileArtifact
from agent_comms.execution.native_output import AssistantOutput, NativeOutputScope, ToolOutput
from agent_comms.execution.native_session_reopen import (
    NativeSessionIdentity,
    NoSessionRetention,
    PiSessionId,
)
from agent_comms.execution.pi_vocabulary import (
    NoThinkingOverride,
    PiStopReason,
    ThinkingLevel,
    ThinkingLevelField,
    UnreportedStopReason,
)
from agent_comms.execution.pi_wire import (
    McpCallPolicy,
    McpServerState,
    Opaque,
    OpenPayload,
    PiDiagnostic,
    PiField,
    PiOmitted,
    PiPayload,
    PiReported,
    PiUsage,
    ReportedInputId,
    TaggedPayload,
    UnknownPayload,
)
from agent_comms.execution.provider_records import (
    CodedProviderDiagnosticError as CodedProviderDiagnosticError,
    CodelessProviderDiagnosticError as CodelessProviderDiagnosticError,
    ImageContent as ImageContent,
    PiContent as PiContent,
    ProviderDiagnosticError as ProviderDiagnosticError,
    ReportedText as ReportedText,
    TextContent as TextContent,
    ThinkingContent as ThinkingContent,
    ToolCallContent as ToolCallContent,
    UnknownContent as UnknownContent,
)
from agent_comms.execution.tool_results import NoSentMessage, NoToolDiff, ToolDiff
from agent_comms.execution.turn_context import (
    ContextTurn,
    MeasuredNativeSegment,
    PreviewProvenance,
    Provenance,
    SegmentManifest,
    TurnContext,
)
from agent_comms.execution.turn_failure import ModelRequestFailed, ProviderTransportFailed
from agent_comms.execution.turn_item import CompactionChargeId, ProviderResponseId, UsageChargeId
from agent_comms.messaging.send_result import CorrectionResult, CorrectionUnreported
from agent_comms.nominal.declared_family import DeclaredFamily, UnknownFamilyMember
from agent_comms.nominal.field_codec import (
    ExternalNull,
    ExternalNullMember,
    FieldCodec,
    InputId,
    MalformedRecord,
    SingleExternalMember,
    SingleFieldValue,
    TextRepresentation,
    ToolCallId,
    WireName,
    WireNullDefault,
    WireOmitDefault,
    WireRequired,
    WireValue,
)
from agent_comms.nominal.lifecycle_capability import Successful
from agent_comms.pi_counts import NonnegativePiCount, PiCount, TotalPiCount, UnreportedCount
from agent_comms.planning.goals import GoalDeclaration
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.thread_model import ThreadModel, UndeclaredModel
from agent_comms.storage.recorded_time import EventMilliseconds, EventTime, UnrecordedEventTime
from agent_comms.transcript.transcript_events import (
    NoticeTranscript,
    SentTranscript,
    ToolEndTranscript,
    TranscriptEvent,
)
from agent_comms.transport.tool_declaration import ToolDeclaration
from agent_comms.transport.tool_result import DeclaredReply


@dataclass(frozen=True)
class NativeMessageFailureSource(FailureSource):
    message: AssistantMessage

    def require_native(self) -> NativeMessageFailureSource:
        return self

    @property
    def retained_in_history(self) -> bool:
        return True

    def for_publication(self, text: str, sensitive: bool) -> FailureSource:
        if not sensitive:
            return self
        # Retain native provenance without exporting private response content.
        return replace(
            self, message=replace(self.message, content=ReportedParts(()), diagnostics=(), error_message=text)
        )


class StrictFields(PiPayload):
    """A closed native record rejects every undeclared external field."""

    @classmethod
    def decode_fields(
        cls, value: dict[str, JsonValue], decoded: dict[str, object],
    ) -> dict[str, object]:
        names = {key for _, key in FieldCodec.record_fields(cls)}
        if set(value) - names - cls.envelope_keys():
            raise MalformedRecord(f"Unexpected {cls.__name__} fields")
        return super().decode_fields(value, decoded)

if TYPE_CHECKING:
    from agent_comms.execution.agent_event import AgentEvent
    from agent_comms.execution.backend import NativeSession, TurnSession
    from agent_comms.execution.native_entries import TranscriptProjection
    from agent_comms.execution.pi_events import AutoRetryEnd
    from agent_comms.execution.transcript_routes import SentTextDigest
    from agent_comms.execution.turn_goal_permission import TurnGoalPermission
    from agent_comms.execution.turn_phase import TurnPhase
    from agent_comms.messaging.routing import Routing


# A Pi payload field is omitted from the wire while it holds its default.


@dataclass(frozen=True)
class ReportedTimestamp(PiReported[int]):
    """The Unix-millisecond clock Pi stamped a record with."""

    value: int


@dataclass(frozen=True)
class ReportedFlag(PiReported[bool]):
    value: bool


@dataclass(frozen=True)
class ProviderRetryOutcome(PiPayload, DeclaredFamily, affix="ProviderRetry"):
    @classmethod
    def decode_external_value(cls, value: object, record: Mapping[str, object]) -> object:
        return super().decode_external_value(record, record)

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

    def apply(self, event: AutoRetryEnd, session: TurnSession) -> tuple[AgentEvent, ...]:
        self.finish_request(session)
        return ()

    @abstractmethod
    def finish_request(self, session) -> None: ...

    @abstractmethod
    def retry_wire(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class SucceededProviderRetry(ProviderRetryOutcome, declared_name="succeeded"):
    def result(self, attempt: int, redact):
        return RecoveredRetry(attempt=attempt)

    def finish_request(self, session) -> None:
        session.request_retried()

    def retry_wire(self) -> dict[str, object]:
        return {"success": True}


@dataclass(frozen=True)
class FailedProviderRetry(ProviderRetryOutcome, declared_name="failed"):
    final_error: Annotated[str, WireName("finalError")]

    def __post_init__(self) -> None:
        if not self.final_error.strip():
            raise ValueError("Failed provider retry end requires finalError")

    def result(self, attempt: int, redact):
        return ExhaustedRetry(attempt=attempt, final_error=redact(self.final_error))

    def apply(self, event, session):
        super().apply(event, session)
        return event.exhausted(session)

    def finish_request(self, session) -> None:
        session.output.request_exhausted(ModelRequestFailed(self.final_error))
        session.finish_request()

    def retry_wire(self) -> dict[str, object]:
        return {"success": False, "finalError": self.final_error}


@dataclass(frozen=True)
class UnreportedUsage(PiUsage):
    """Pi sent no usage object: nothing is charged and no token total is known."""

    def charged(self, identity: UsageChargeId | PiOmitted) -> tuple[UsageChargeId, ...]:
        return ()


@dataclass(frozen=True)
class ReportedParts(PiReported[tuple[PiContent, ...]]):
    """Pi supplied an array; an empty array remains reported, not omitted."""

    value: tuple[PiContent, ...]

    @property
    def message_parts(self) -> tuple[PiContent, ...]:
        return self.value

    def rendered_text(self, separator: str) -> str:
        return separator.join(part.text for part in self.value if part.text)

    def preserve_evidence(self, raw: object) -> Self:
        # The field decoder established the array shape. Preserve extension
        # fields from that same input without decoding or type-switching again.
        raw_parts = typing.cast(list[dict[str, JsonValue]], raw)
        return replace(self, value=tuple(
            part.preserve_evidence(raw_part)
            for part, raw_part in zip(self.value, raw_parts, strict=True)
        ))


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


class TransportStageText(TextRepresentation[ProviderTransportStage]):
    """The native provider's scalar spelling, not a second record decoder."""

    @classmethod
    def encode(cls, value: ProviderTransportStage) -> str:
        return value.to_text()

    @classmethod
    def from_text(cls, value: str) -> ProviderTransportStage:
        return ProviderTransportStage.from_text(value)


class TransportFact(WireValue):
    """One optional provider transport observation; each fact renders its own description part."""

    __slots__ = ()

    def parts(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True, slots=True)
class UnreportedTransportFact(PiOmitted, TransportFact):
    """Pi left this transport observation out."""


@dataclass(frozen=True)
class ConfiguredTransport(SingleFieldValue, TransportFact):
    name: str

    def parts(self) -> tuple[str, ...]:
        return (f"configured transport: {self.name}",)


@dataclass(frozen=True)
class FallbackTransport(SingleFieldValue, TransportFact):
    name: str

    def parts(self) -> tuple[str, ...]:
        return (f"reported fallback transport: {self.name}",)


@dataclass(frozen=True)
class EventsEmitted(SingleFieldValue, TransportFact):
    emitted: bool

    def parts(self) -> tuple[str, ...]:
        return ("provider events emitted: " + ("yes" if self.emitted else "no"),)


@dataclass(frozen=True)
class RequestBytes(SingleFieldValue, TransportFact):
    count: int

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("Native request byte count must be nonnegative")

    def parts(self) -> tuple[str, ...]:
        return (f"request: {self.count:,} bytes",)


@dataclass(frozen=True)
class ProviderTransportDetails(PiPayload):
    configured_transport: Annotated[
        PiField[ConfiguredTransport | UnreportedTransportFact], WireName("configuredTransport")
    ] = UnreportedTransportFact()
    fallback_transport: Annotated[
        PiField[FallbackTransport | UnreportedTransportFact], WireName("fallbackTransport")
    ] = UnreportedTransportFact()
    events_emitted: Annotated[
        PiField[EventsEmitted | UnreportedTransportFact], WireName("eventsEmitted")
    ] = UnreportedTransportFact()
    phase: Annotated[ProviderTransportStage, TransportStageText] = field(
        default_factory=UnreportedTransportStage
    )
    request_bytes: Annotated[
        PiField[RequestBytes | UnreportedTransportFact], WireName("requestBytes")
    ] = UnreportedTransportFact()

    @property
    def description(self) -> str:
        return "; ".join((
            self.phase.description,
            *self.configured_transport.parts(),
            *self.events_emitted.parts(),
            *self.request_bytes.parts(),
            *self.fallback_transport.parts(),
        ))


@dataclass(frozen=True)
class ProviderTransportFailureDiagnostic(PiDiagnostic):
    error: ProviderDiagnosticError

    def connection_failure_description(self) -> tuple[str, ...]:
        return (self.description,)
    details: ProviderTransportDetails
    timestamp: PiField[ReportedTimestamp | PiOmitted] = PiOmitted()

    def turn_phase(self, phase: TurnPhase) -> TurnPhase:
        from agent_comms.execution.turn_phase import FailedPhase
        return FailedPhase(error=self.error)

    def refine_model_request_failure(self, failure):
        return ProviderTransportFailed(failure.text)

    @property
    def description(self) -> str:
        return "; ".join(
            part for part in (self.error.description, self.details.description) if part
        )


@dataclass(frozen=True)
class UnknownDiagnostic(UnknownPayload, PiDiagnostic):
    pass


@dataclass(frozen=True)
class PiMessage(WithheldAnnotations, ExternalNull, OpenPayload, affix="Message"):
    @property
    def parts(self) -> tuple[PiContent, ...]:
        return self.content.message_parts

    @property
    def tool_calls(self) -> tuple[ToolCallContent, ...]:
        """The tool calls this message makes, in the order it makes them."""
        return tuple(call for part in self.parts for call in part.tool_calls())

    def matches_input(self, text: str, native_id: InputId | PiOmitted) -> bool:
        return False

    def committed_outputs(self) -> tuple[NativeOutputScope, ...]:
        return ()

    def storage_refusals(self) -> tuple[tuple[str, StorageDenialToolDetails], ...]:
        return ()

    def said(self) -> tuple[str, ...]:
        """What the model said to its reader in this message; most messages carry none."""
        return ()

    @property
    def unread_reply(self) -> bool:
        return False

    def transcript_events(self, context, timestamp: float):
        return []

    wire_tag = "role"
    part_separator: ClassVar[str] = ""

    @property
    def final_reply(self) -> bool:
        return False
    content: ReportedText | ReportedParts | PiOmitted = PiOmitted()
    usage: Annotated[PiField[PiUsage], WireNullDefault()] = UnreportedUsage()
    input_id: Annotated[PiField[ReportedInputId | PiOmitted], WireName("inputId")] = PiOmitted()
    input_digest: Annotated[
        PiField[ReportedText | PiOmitted], WireName("inputDigest")
    ] = PiOmitted()
    stop_reason: Annotated[
        PiField[type[PiStopReason]], WireName("stopReason")
    ] = UnreportedStopReason
    error_message: Annotated[
        PiField[ReportedText | PiOmitted], WireName("errorMessage")
    ] = PiOmitted()
    diagnostics: tuple[PiDiagnostic, ...] = ()
    timestamp: Annotated[PiField[EventTime], EventMilliseconds] = UnrecordedEventTime()

    @property
    def measured_tokens(self) -> PiCount:
        return self.stop_reason.measured(self.usage.total_tokens)

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
        return False

    @property
    def update_context(self):
        return False

    def is_user_message(self) -> bool:
        return False

    def preserve_evidence(self, raw: Mapping[str, object]) -> Self:
        return self

    @property
    def text(self) -> str:
        return self.content.rendered_text(self.part_separator)


class AbsentMessage(ExternalNullMember, PiMessage):
    """An omitted external message permits neither new input nor output."""

    update_context = True


@dataclass(frozen=True)
class AssistantMessage(PublicInstructionAnnotations, PiMessage):
    # Pi's assistant record always carries an array, including failed terminals.
    content: Annotated[ReportedParts, WireRequired()] = ReportedParts(())
    # Original Pi completion metadata. These are observations, not registry
    # configuration or permission to select a model for a later request.
    api: PiField[str | PiOmitted] = PiOmitted()
    provider: PiField[str | PiOmitted] = PiOmitted()
    model: PiField[str | PiOmitted] = PiOmitted()
    response_model: Annotated[
        PiField[str | PiOmitted], WireName("responseModel")
    ] = PiOmitted()
    response_id: Annotated[PiField[ProviderResponseId | PiOmitted], WireName("responseId")] = PiOmitted()
    provider_thinking_level: Annotated[
        PiField[str | PiOmitted], WireName("providerThinkingLevel")
    ] = PiOmitted()
    @property
    def invalidates_start(self):
        return True

    @property
    def update_context(self):
        return True

    def committed_outputs(self) -> tuple[NativeOutputScope, ...]:
        return (AssistantOutput(),)

    def said(self) -> tuple[str, ...]:
        """The text parts the reader saw; a tool call or hidden reasoning shows none."""
        return tuple(text for part in self.parts for text in part.text_parts() if text.strip())

    @property
    def authoritative_text(self) -> str:
        return "".join(part.final_text for part in self.parts)

    @property
    def final_reply(self):
        return bool(self.stop_reason.items_with(Successful, self))

    async def apply_start(self, session, event):
        await session.assistant_started()
        return ()

    def require_failed_terminal(self):
        self.stop_reason.require_failed_terminal()

    async def apply_end(self, session):
        reason = self.stop_reason
        updates = []
        if reason.tool_round and session.committable_message(self.text):
            updates.append(events.CommittedProgress(text=self.text))
        session.output.start_message()
        if session.accepts_output:
            updates.extend(events.ProviderUsage.charges(self.usage, self.response_id))
        session.output.final_assistant_stop = (
            bool(reason.items_with(Successful, reason)) and session.started_input
        )
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
        if notice := self.error_notice:
            return [NoticeTranscript(f"[agent error] {notice}", position=context.position, timestamp=timestamp)]
        events = []
        for part in self.parts:
            for event in part.assistant_transcript(context.position, timestamp):
                event.append_to(events)
        return [replace(event, routing=context.routing) for event in events]


class UserMessage(PiMessage):
    def is_user_message(self) -> bool:
        return True

    @property
    def invalidates_start(self):
        return True
    part_separator = "\n"

    def preserve_evidence(self, raw: Mapping[str, object]) -> Self:
        return replace(self, content=self.content.preserve_evidence(raw.get("content")))

    def matches_input(self, text: str, native_id: InputId | PiOmitted) -> bool:
        # An omitted input id matches an omitted prompt id, as it always has.
        return self.text == text and self.input_id.value_or(PiOmitted()) == native_id

    async def apply_start(self, session, event):
        return await session.accept_user_message(self, event)

    def observe_start_abort(self, session, event):
        session.observe_input_during_abort(event)

    @property
    def original_text(self) -> str:
        # Preserve exact separators, including empty text blocks, for provenance.
        return "\n".join(text for part in self.parts for text in part.text_parts())

    def matches_display(self, digest: SentTextDigest) -> bool:
        return self.content.is_reported() and digest.matches(self.original_text)

    def display_parts(self, text: str) -> tuple[PiContent, ...]:
        return (
            TextContent(text),
            *(retained for part in self.parts
              for retained in part.with_display_text(text).present()),
        )

    def transcript_events(self, context, timestamp: float):
        return context.input_display.project_message(self, context, timestamp)

    def project_parts(
        self, parts: tuple[PiContent, ...], context: TranscriptProjection,
        timestamp: float, routing: Routing,
    ) -> list[TranscriptEvent]:
        events = [event for part in parts for event in part.user_transcript(context.position, timestamp)]
        events = [replace(event, routing=routing) for event in events]
        return [event.with_native_input(self.input_id) for event in events]


@dataclass(frozen=True)
class ToolResultMessage(PiMessage, declared_name="toolResult"):
    tool_call_id: Annotated[PiField[ToolCallId], WireName("toolCallId")] = field(kw_only=True)
    tool_name: Annotated[PiField[str], WireName("toolName")] = "tool"
    is_error: Annotated[PiField[bool], WireName("isError")] = False
    details: NativeToolDetails = field(default_factory=lambda: NoToolDetails())

    def committed_outputs(self) -> tuple[NativeOutputScope, ...]:
        return (ToolOutput(self.tool_call_id),)

    def storage_refusals(self) -> tuple[tuple[str, StorageDenialToolDetails], ...]:
        if not self.is_error:
            return ()
        return tuple((self.tool_call_id, denial) for denial in self.details.storage_refusals())

    def transcript_events(self, context, timestamp: float):
        if not self.parts:
            return []
        output = "\n".join(text for part in self.parts for text in part.text_parts())
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
class UnknownMessage(UnknownPayload, PiMessage):
    pass


@dataclass(frozen=True)
class PiDelta(OpenPayload, affix="Delta"):
    progress: ClassVar[bool] = False
    response_progress: ClassVar[bool] = False

    def events(self):
        return ()

    async def emit(self, session):
        return self.events()


@dataclass(frozen=True)
class NoDelta(PiOmitted, PiDelta, declared_name=None):
    """The update carried no assistant event: no events and no progress, as PiDelta answers."""


@dataclass(frozen=True)
class ContentDelta(PiDelta, declared_name=None):
    delta: str

    @property
    def progress(self):
        return bool(self.delta)


@dataclass(frozen=True)
class TextDelta(ContentDelta, declared_name="text_delta"):
    response_progress = True

    def events(self):
        return (events.Chunk(text=self.delta),)

    async def emit(self, session):
        await session.text_received(self.delta)
        return self.events()


@dataclass(frozen=True)
class ThinkingDelta(ContentDelta, declared_name="thinking_delta"):
    def events(self):
        return (events.Thinking(text=self.delta),) if self.delta else ()


class ToolDelta:
    progress = True
    response_progress = True


class ToolcallStartDelta(ToolDelta, PiDelta, declared_name="toolcall_start"):
    pass


class ToolcallDelta(ToolDelta, PiDelta, declared_name="toolcall_delta"):
    pass


class ToolcallEndDelta(ToolDelta, PiDelta, declared_name="toolcall_end"):
    pass


@dataclass(frozen=True, kw_only=True)
class UnknownDelta(UnknownPayload, PiDelta):
    pass


@dataclass(frozen=True)
class PiModel(SingleExternalMember, PiPayload, DeclaredFamily, affix="Model"):
    context_window: ClassVar[PiCount] = UnreportedCount()

    def thread_model(self) -> ThreadModel:
        return UndeclaredModel()

    def require_selection(self, selected: ThreadModel):
        raise ValueError("Prepared native model does not match the owner selection")


class UnreportedModel(ExternalNullMember, PiModel):
    """The external state has not reported a model; no identity is implied."""


@dataclass(frozen=True)
class ReportedModel(PiModel):
    provider: PiField[ReportedText | PiOmitted] = PiOmitted()
    id: PiField[ReportedText | PiOmitted] = PiOmitted()
    name: PiField[ReportedText | PiOmitted] = PiOmitted()
    context_window: Annotated[
        PiField[PiCount], WireName("contextWindow")
    ] = UnreportedCount()
    max_tokens: Annotated[
        PiField[PiCount], WireName("maxTokens")
    ] = UnreportedCount()

    def require_selection(self, selected: ThreadModel):
        if self.thread_model() != selected:
            return super().require_selection(selected)
        return self

    def thread_model(self) -> ThreadModel:
        """Provider and the model's id (else its name), as far as Pi reported them."""
        shown = (*self.id.present(), *self.name.present())[:1]
        return ThreadModel.reported("/".join((*self.provider.present(), *shown)) or None)

    def catalog_texts(self) -> tuple[str, ...]:
        """A catalog lists a model only when Pi named both its provider and its id."""
        return tuple(text for _provider in self.provider.present() for _id in self.id.present()
                     for text in self.thread_model().texts())


@dataclass(frozen=True)
class PiContextUsage(PiPayload):
    tokens: PiField[TotalPiCount] = UnreportedCount()
    context_window: Annotated[
        PiField[PiCount], WireName("contextWindow")
    ] = UnreportedCount()

    @property
    def token_total(self) -> PiCount:
        return self.tokens


class SnapshotAttestation(ABC):
    """The identity-conflict contract a native snapshot can ask its attestation."""

    @abstractmethod
    def conflicts(self, data: NativeSessionSnapshot) -> bool: ...


@dataclass(frozen=True)
class PiResponseData(ExternalNull, PiPayload, DeclaredFamily, affix="Data"):

    @property
    def session_busy(self) -> bool:
        return False

    def require_payload(self) -> PiResponseData:
        return self

    def require_state(self) -> StateData:
        """Only Pi's session-state reply answers a state query."""
        raise ValueError(f"{type(self).__name__} is not Pi's session state")

    def require_compaction(self) -> CompactionData:
        """Only Pi's compact reply supplies a compaction result."""
        raise ValueError(f"{type(self).__name__} is not Pi's compaction result")

    def require_context(self) -> NativeContextData:
        """Only Pi's context inspection reply supplies native context sources."""
        raise ValueError(f"{type(self).__name__} is not Pi's native context")

    def native_updates(self, session: NativeSession) -> Iterable[AgentEvent]:
        return ()

    def cleared_inputs(self, requested: tuple[InputId, ...]) -> tuple[InputId, ...]:
        return ()

    def conflicts_attestation(self, attestation: SnapshotAttestation) -> bool:
        return False

    @classmethod
    def wire_member(cls, value):
        return cls if cls is not PiResponseData else cls.member_named(value.get("kind"))


@dataclass(frozen=True)
class NativeContextData(StrictFields, PiResponseData, DeclaredReply):
    counter: str
    identity: NativeSessionIdentity
    segments: tuple[MeasuredNativeSegment, ...]

    def require_context(self) -> NativeContextData:
        return self

    def observation(self) -> PreviewProvenance:
        """Return the SDK's original observation, without deriving its digest."""
        originals = {observation for segment in self.segments
                     for source in segment.provenance
                     for observation in source.preview_observations()}
        if len(originals) != 1:
            raise ValueError("Native context has no unambiguous current preview observation")
        (original,) = originals
        self.identity.require_same_session(original.identity)
        return original

    def public_source(
        self, observation: PreviewProvenance, segment: int, source: Provenance,
    ) -> Provenance:
        if observation != self.observation():
            raise ValueError("Current native preview changed since the selected observation")
        if not 0 <= segment < len(self.segments):
            raise ValueError("Source has no selected context segment")
        return self.segments[segment].require_source(source)

    def recorded_public_text(self, expected: SegmentManifest) -> str:
        self.identity.require_same_session(expected.sources.native_identity())
        originals: dict[str, list[SegmentManifest]] = {}
        for original in expected.original_values():
            originals.setdefault(original.sha256, []).append(original)
        values: dict[str, list[MeasuredNativeSegment]] = {}
        for segment in self.segments:
            if not any(segment.matches_recorded(original)
                       for original in originals.get(segment.sha256, ())):
                raise ValueError("Recorded SDK source is outside the selected original value")
            values.setdefault(segment.sha256, []).append(segment)
        return expected.recorded_public_text(values)

    def for_turn(self, owner: ThreadIncarnation, turn: ContextTurn) -> TurnContext:
        return TurnContext(owner, turn, self.segments)

    def require_session_file(self, session_file: str) -> NativeContextData:
        if self.identity.session_file != session_file:
            raise ValueError("Context inspection belongs to a different selected native session")
        return self


@dataclass(frozen=True)
class QueueClearedData(PiResponseData):
    """Native IDs actually removed, never the texts of possible candidates."""

    input_ids: Annotated[tuple[InputId, ...], WireName("inputIds")]

    def cleared_inputs(self, requested: tuple[InputId, ...]) -> tuple[InputId, ...]:
        if len(set(self.input_ids)) != len(self.input_ids) or not set(self.input_ids) <= set(requested):
            raise ValueError("Native clear returned duplicate or unrequested input IDs")
        return self.input_ids


@dataclass(frozen=True)
class SteeringInterruptData(PiResponseData):
    """Pi's answer to Send now: whether it stopped the run to place the steering inputs."""

    interrupted: bool


class EmptyData(PiResponseData):
    pass


class MissingData(ExternalNullMember, PiResponseData):
    """An omitted/null native payload is not a successful data observation."""

    def require_payload(self) -> typing.Never:
        raise ValueError("Native response has no data")


@dataclass(frozen=True)
class SessionSwitchData(StrictFields, PiResponseData):
    """The SDK's actual session-replacement outcome, not a local readiness flag."""
    cancelled: bool


@dataclass(frozen=True)
class NativeSessionSnapshot(PiPayload):
    """External snapshots may report only part of a saved session's identity."""

    session_id: Annotated[PiField[ReportedText | PiOmitted], WireName("sessionId")] = PiOmitted()
    session_file: Annotated[
        PiField[ReportedText | PiOmitted], WireName("sessionFile")
    ] = PiOmitted()

    def _known_identity(self) -> dict[str, str]:
        return {
            component.name: value
            for component in fields(NativeSessionIdentity)
            if (value := getattr(self, component.name).text_or(""))
        }

    @property
    def identity(self) -> NativeSessionIdentity | NoSessionRetention:
        known = self._known_identity()
        if len(known) != len(fields(NativeSessionIdentity)):
            return NoSessionRetention()
        return NativeSessionIdentity(
            FieldCodec.decode(PiSessionId, self.session_id.text_or("")),
            self.session_file.text_or(""),
        )

    def conflicts(self, other: NativeSessionSnapshot) -> bool:
        known, observed = self._known_identity(), other._known_identity()
        return any(known[key] != observed[key] for key in known.keys() & observed.keys())

    def conflicts_attestation(self, attestation: SnapshotAttestation) -> bool:
        return attestation.conflicts(self)


@dataclass(frozen=True)
class StateData(NativeSessionSnapshot, PiResponseData):
    def require_state(self) -> StateData:
        return self

    native_input_proof_capability: Annotated[
        PiField[str | PiOmitted], WireName("nativeInputProofCapability")
    ] = PiOmitted()
    # Native omission and null both mean no model observation. Omit the named
    # default on output; its internal family tag is not a native model object.
    model: Annotated[PiModel, WireOmitDefault()] = field(
        default_factory=UnreportedModel
    )
    thinking_level: Annotated[
        PiField[ThinkingLevelField], WireName("thinkingLevel")
    ] = NoThinkingOverride
    message_count: Annotated[
        PiField[NonnegativePiCount], WireName("messageCount")
    ] = UnreportedCount()
    pending_message_count: Annotated[
        PiField[NonnegativePiCount], WireName("pendingMessageCount")
    ] = UnreportedCount()
    is_streaming: Annotated[
        PiField[ReportedFlag | PiOmitted], WireName("isStreaming")
    ] = PiOmitted()
    is_compacting: Annotated[
        PiField[ReportedFlag | PiOmitted], WireName("isCompacting")
    ] = PiOmitted()

    def native_updates(self, session: NativeSession) -> Iterable[AgentEvent]:
        session.native.attestation = session.native.attestation.observe(self)
        session.active_session_file = self.session_file.text_or(session.active_session_file)
        self.model.context_window.observe_size(session.usage)
        yield self.agent_info(session)

    def agent_info(self, session):
        """Project this actual native state, not a later usage-only observation."""
        return events.AgentInfo(
            model=self.model.thread_model(),
            thinking_level=self.thinking_level,
            session_file=session.active_session_file,
            context_size=self.model.context_window,
        )

    @property
    def session_busy(self) -> bool:
        return self.is_streaming.require() or self.is_compacting.require()


@dataclass(frozen=True)
class SessionStatsData(NativeSessionSnapshot, PiResponseData):
    context_usage: Annotated[
        PiField[ReportedContextUsage | PiOmitted], WireName("contextUsage")
    ] = PiOmitted()

    def native_updates(self, session: NativeSession) -> Iterable[AgentEvent]:
        for context in self.context_usage.present():
            context.token_total.confirm(session.usage)
            if not session.native.attestation.uncertain:
                context.context_window.update_size(session.usage)
        yield session.context_info()


@dataclass(frozen=True)
class ReportedContextUsage(PiReported[PiContextUsage]):
    value: PiContextUsage


@dataclass(frozen=True)
class ModelsData(PiResponseData):
    models: tuple[PiModel, ...] = ()


@dataclass(frozen=True)
class ThinkingLevelsData(PiResponseData):
    levels: tuple[type[ThinkingLevel], ...] = ()


@dataclass(frozen=True)
class CompactionData(PiResponseData):
    def require_compaction(self) -> CompactionData:
        return self

    summary: PiField[ReportedText | PiOmitted] = PiOmitted()
    usage: Annotated[PiField[PiUsage], WireNullDefault()] = UnreportedUsage()
    first_kept_entry_id: Annotated[
        PiField[ReportedText | PiOmitted], WireName("firstKeptEntryId")
    ] = PiOmitted()
    tokens_before: Annotated[
        PiField[PiCount], WireName("tokensBefore")
    ] = UnreportedCount()
    estimated_tokens_after: Annotated[
        PiField[PiCount], WireName("estimatedTokensAfter")
    ] = UnreportedCount()

    def charge(self):
        yield from events.ProviderUsage.charges(self.usage, CompactionChargeId.fresh())


@dataclass(frozen=True)
class ReportedCompaction(PiReported[CompactionData]):
    value: CompactionData


@dataclass(frozen=True)
class UnknownData(Opaque, PiResponseData):
    @classmethod
    def decode_external_value(cls, value: object, record: Mapping[str, object]) -> object:
        return FieldCodec.construct(cls, {"payload": FieldCodec.decode(JsonValue, value)})


@dataclass(frozen=True)
class PiToolResult(SingleExternalMember, PiPayload, DeclaredFamily, affix="ToolResult"):
    @abstractmethod
    def text(self, limit=4000): ...

    def published_goal(self, permission: TurnGoalPermission) -> None:
        pass

    def edit_diff(self, ok):
        return NoToolDiff()

    def sent_message(self, ok):
        return NoSentMessage()


class MissingToolResult(ExternalNullMember, PiToolResult):
    """No native result was emitted; output and edit evidence are absent."""

    def text(self, limit=4000):
        return ""


@dataclass(frozen=True)
class ProvidedToolResult(PiToolResult):
    content: tuple[PiContent, ...] = ()
    details: NativeToolDetails = field(default_factory=lambda: NoToolDetails())

    def text(self, limit=4000):
        text = "".join(part.text for part in self.content)
        return text[:limit] + ("…" if len(text) > limit else "")

    def edit_diff(self, ok):
        return self.details.edit_diff() if ok else NoToolDiff()

    def sent_message(self, ok):
        return self.details.sent_message() if ok else NoSentMessage()

    def published_goal(self, permission: TurnGoalPermission) -> None:
        self.details.published_goal(permission)


@dataclass(frozen=True)
class NativeEditDetails(PiPayload):
    """Native edit metadata, decoded only when projecting actual edit evidence."""

    patch: PiField[ReportedText | PiOmitted] = PiOmitted()
    diff: PiField[ReportedText | PiOmitted] = PiOmitted()
    first_changed_line: Annotated[
        PiField[int | PiOmitted], WireName("firstChangedLine")
    ] = PiOmitted()

    @classmethod
    def capture_diff(cls, raw):
        try:
            details = cls.from_wire(raw)
        except MalformedRecord:
            # Extension-defined metadata may be opaque. Preserve the original
            # result and output; unsupported formatting grants no diff evidence.
            return NoToolDiff()
        return details.reported_diff()

    def reported_diff(self):
        if (patch := self.patch.value_or("")).strip():
            return ToolDiff(patch)
        if (diff := self.diff.value_or("")).strip():
            # This is the external numbered edit format in retained Pi journals.
            return ToolDiff(diff, "numbered")
        return NoToolDiff()


@dataclass(frozen=True)
class NativeToolDetails(ExternalNull, TaggedPayload, DeclaredFamily, affix="ToolDetails"):
    """Decode the original owned result once; other extension details stay opaque."""
    wire_tag = "agentCommsKind"

    @classmethod
    def decode_external_value(cls, value: object, record: Mapping[str, object]) -> object:
        if not isinstance(value, dict) or cls.wire_tag not in value:
            return FieldCodec.construct(OpaqueToolDetails, {
                "payload": FieldCodec.decode(JsonValue, value),
            })
        return super().decode_external_value(value, record)

    @classmethod
    def wire_member(cls, value):
        return cls.member_named(value[cls.wire_tag])

    def edit_diff(self):
        return NoToolDiff()

    def sent_message(self):
        return NoSentMessage()

    def published_goal(self, permission: TurnGoalPermission) -> None:
        pass

    def storage_refusals(self) -> tuple[StorageDenialToolDetails, ...]:
        return ()


@dataclass(frozen=True)
class StorageDenialToolDetails(NativeToolDetails):
    """The owner preflight's refusal, carried in the actual native tool result."""

    reason: str

    def storage_refusals(self) -> tuple[StorageDenialToolDetails, ...]:
        return (self,)


@dataclass(frozen=True, kw_only=True)
class PublishedMessageToolDetails(StrictFields, NativeToolDetails):
    """The send owner supplies its original committed message, not a guessed acknowledgement."""
    id: str
    message: SentTranscript
    correction: CorrectionResult = field(default_factory=CorrectionUnreported)

    def __post_init__(self):
        if self.id != self.message.source.message_id:
            raise ValueError("Published tool result belongs to another message")

    def sent_message(self):
        return self.message


@dataclass(frozen=True)
class PublishedGoalToolDetails(NativeToolDetails):
    """The declaration actually returned by a self-relative goal-writing tool."""

    goal: GoalDeclaration

    def published_goal(self, permission: TurnGoalPermission) -> None:
        permission.published(self.goal)


class NoToolDetails(ExternalNullMember, NativeToolDetails):
    pass


@dataclass(frozen=True)
class OpaqueToolDetails(Opaque, NativeToolDetails):
    def edit_diff(self):
        return NativeEditDetails.capture_diff(self.payload)


@dataclass(frozen=True, kw_only=True)
class FileMutationToolDetails(StrictFields, NativeToolDetails, NativeEditDetails):
    """Known native publisher metadata, retaining the existing edit evidence."""
    artifact: Annotated[NativeFileArtifact, WireName("agentCommsArtifact")]

    def edit_diff(self):
        return self.reported_diff()


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


class ReadyMcpServerState(McpServerState):
    @classmethod
    def validate(cls, calls: type[McpCallPolicy], counts: tuple[int, ...]) -> None:
        calls.require_available()


class ErrorMcpServerState(McpServerState):
    @classmethod
    def validate(cls, calls: type[McpCallPolicy], counts: tuple[int, ...]) -> None:
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


