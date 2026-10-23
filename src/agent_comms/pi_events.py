"""Pi's open event vocabulary, decoded by the RPC channel before execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, ClassVar

from . import agent_events as events
from . import turn_phase as phases
from .acp_failure import UnrecordedFailureSource
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, FieldPlan, WireName, WireOmitDefault
from .native_input_record import NativeInputCommit, NativeInputLocation
from .native_pi import NativePiOperationFailed
from .native_turn_context import NativeContextManifestData
from .pi_commands import ExtensionUiResponse, PiCommand, UnknownCommand
from .pi_payloads import (
    AbsentMessage,
    AnonymousRequest,
    CompactionData,
    MissingData,
    MissingToolResult,
    NoDelta,
    Opaque,
    OpenPayload,
    PiDelta,
    PiMessage,
    PiOmitted,
    PiRequestId,
    PiResponseData,
    PiToolResult,
    PiUsage,
    ProviderRetryOutcome,
    ReportedCompaction,
    ReportedFlag,
    ReportedText,
    UnknownPayload,
    UnreportedUsage,
)
from .pi_vocabulary import CompactionReason, UnknownCompactionReason
from .request_progress import RequestProgress
from .tool_declaration import ToolDeclaration

if TYPE_CHECKING:
    from .backend import TurnSession


class RetryProgress:
    @property
    def retry_progress(self) -> bool:
        return True




@dataclass(frozen=True)
class PiEvent(OpenPayload, affix=""):
    """One declaration-owned record received by the Pi RPC channel."""

    starts: ClassVar[type[phases.TurnPhase] | None] = None
    ends: ClassVar[tuple[type[phases.TurnPhase], ...]] = ()

    def on(self, current: phases.TurnPhase) -> phases.TurnPhase:
        if self.starts is not None:
            return self.enter(self.starts, current)
        return self.leave(current) if self.matches_end(current) else current

    def enter(self, member: type[phases.TurnPhase], current: phases.TurnPhase) -> phases.TurnPhase:
        return member()

    def matches_end(self, current: phases.TurnPhase) -> bool:
        return isinstance(current, self.ends)

    def leave(self, current: phases.TurnPhase) -> phases.TurnPhase:
        return phases.ModelWaitPhase()

    def __init_subclass__(
        cls, *, starts: type[phases.TurnPhase] | None = None,
        ends: type[phases.TurnPhase] | None = None, **kwargs,
    ) -> None:
        super().__init_subclass__(**kwargs)
        if starts is not None:
            cls.starts = starts
        if ends is not None:
            cls.ends = (*cls.ends, ends)

    @staticmethod
    def unowned_event(event):
        """Idle native queries may interleave unsolicited Pi notifications."""

    async def deliver_to(self, channel) -> None:
        await channel.deliver_event(self)

    @classmethod
    def known_kinds(cls):
        return (cls.wire_kind(),)

    @classmethod
    def wire_kind(cls) -> str:
        """The Pi ``type`` this member converts."""
        return cls.declared_name

    def convert(self):
        return ()

    async def consume(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        """Apply shared progress and phase behavior around this event's meaning."""
        async for event in session.consume_native_event(self):
            yield event

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        await self.act(session)
        for event in self.convert():
            yield event

    async def act(self, session: TurnSession) -> None:
        pass

    @property
    def invalidates_stop(self) -> bool:
        return False

    @property
    def retry_progress(self) -> bool:
        return False

    def observe_abort(self, session: TurnSession) -> None:
        pass

    def observe_request(self, observer: Callable[[RequestProgress], None]) -> bool:
        """A transport measurement is independent of turn-phase publication."""
        return False

    def responds_to(self, request: PiCommand) -> bool:
        return False


class NativePiEvent(PiEvent, declared_name=None):
    pass


class DurablePiEvent(PiEvent, declared_name=None):
    @staticmethod
    def unowned_event(event):
        raise RuntimeError('Pi event arrived with no open turn')

    @classmethod
    def wire_member(cls, wire):
        members = tuple(member for member in cls.members_with(cls)
                        if wire.get("type") in member.known_kinds())
        if len(members) > 1:
            raise ValueError("Ambiguous durable event declaration")
        return members[0] if members else UnknownPiEvent


class IgnoredDurableEvent(Opaque, DurablePiEvent):
    ignored_kinds = ('run_start', 'run_end', 'inbox_update', 'submission', 'deferred_poll', 'usage_changed', 'snapshot', 'agent_changed')

    @classmethod
    def known_kinds(cls):
        return cls.ignored_kinds


@dataclass(frozen=True)
class UnknownPiEvent(UnknownPayload, NativePiEvent, DurablePiEvent):
    """Ignore unrecognized events; they do not prove permission to start input."""

    @classmethod
    def known_kinds(cls):
        return ()


@dataclass(frozen=True)
class TurnStart(Opaque, NativePiEvent, DurablePiEvent):
    """Pi starts one model round. Core follows the run, not its rounds."""


@dataclass(frozen=True)
class TurnEnd(Opaque, NativePiEvent, DurablePiEvent):
    """Pi ends one model round. Settlement comes from agent_end / agent_settled."""


@dataclass(frozen=True)
class QueueUpdate(Opaque, NativePiEvent):
    """Pi's own steering queue changed. Core keeps the queue the user sees."""


@dataclass(frozen=True)
class EntryAppended(Opaque, NativePiEvent, DurablePiEvent):
    """Pi wrote a session entry. History is read from the session file."""


@dataclass(frozen=True)
class SessionInfoChanged(Opaque, NativePiEvent):
    """The session name changed. Core reads it from get_state."""


@dataclass(frozen=True)
class ThinkingLevelChanged(Opaque, NativePiEvent):
    """The thinking level changed. Core reads it from get_state."""


@dataclass(frozen=True)
class BashExecutionUpdate(Opaque, NativePiEvent):
    """Output of a user ``!`` bash command; Core never sends one."""


@dataclass(frozen=True)
class ExtensionError(Opaque, NativePiEvent):
    """An extension hook failed inside Pi; the failure stays in Pi's own handling."""


@dataclass(frozen=True, kw_only=True)
class AgentEnd(phases.AcceptsPrompt, NativePiEvent):
    will_retry: Annotated[ReportedFlag | PiOmitted, WireName("willRetry")] = PiOmitted()

    @property
    def invalidates_stop(self) -> bool:
        return self.will_retry.value_or(False)


@dataclass(frozen=True, kw_only=True)
class NativeOperationError(NativePiEvent):
    """The native run refused an operation after the prompt was acknowledged."""

    error: str

    async def act(self, session: TurnSession) -> None:
        raise NativePiOperationFailed(self)


class AgentSettled(RetryProgress, NativePiEvent):
    def settles(self, session: TurnSession) -> bool:
        # A prior retained turn may finish while the new state query is
        # pending. It cannot settle a turn whose input start is unconfirmed.
        return not session.awaiting_native_attestation

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        if not self.settles(session):
            session.skip = True
            return
        for event in await session.agent_settled(self):
            yield event


class AgentStart(phases.AcceptsPrompt, NativePiEvent):

    @property
    def invalidates_stop(self) -> bool:
        return True


@dataclass(frozen=True, kw_only=True)
class ModelRequestProgress(NativePiEvent):
    progress: "RequestProgress"

    def observe_request(self, observer: Callable[[RequestProgress], None]) -> bool:
        observer(self.progress)
        return True


@dataclass(frozen=True, kw_only=True)
class RetryEnd(PiEvent, declared_name=None):
    attempt: int

    def __post_init__(self) -> None:
        if self.attempt < 0:
            raise ValueError("Retry count must be nonnegative")


@dataclass(frozen=True, kw_only=True)
class AutoRetryEnd(RetryEnd, NativePiEvent, ends=phases.ProviderRetryPhase):
    outcome: Annotated[ProviderRetryOutcome, WireName("success")]

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.watchdog.progress()
        updates = session.provider_retry_ended(self)
        for event in updates:
            yield event

    def exhausted(self, session: TurnSession) -> tuple[events.AgentEvent, ...]:
        return (session.output.error(session.output.declared_failure.text,
                                     source=session.output.native_failure_source),)

    def to_wire(self):
        return {
            "type": self.wire_kind(),
            "attempt": self.attempt,
            **self.outcome.retry_wire(),
        }


class NativeRetryEnd(AutoRetryEnd):
    """The retry outcome owns whether native recovery completes the request."""

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        async for event in super().apply(session):
            yield event
        for event in self.outcome.recovery_events(session):
            yield event

    def exhausted(self, session: TurnSession) -> tuple[events.AgentEvent, ...]:
        return ()


@dataclass(frozen=True, kw_only=True)
class RetryAttemptEvent(NativePiEvent):
    """Original Pi retry data and shared observation behavior.

    This intermediate declaration factors the same fields and algorithm from
    three external events. It neither authorizes replay nor stores an attempt
    separate from the event that Pi emitted.
    """

    attempt: int | PiOmitted = PiOmitted()
    max_attempts: Annotated[int | PiOmitted, WireName("maxAttempts")] = PiOmitted()

    @property
    @abstractmethod
    def retry_reason_code(self) -> str: ...

    def prepare_retry(self, session: TurnSession) -> None:
        pass

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        elapsed_ms = round(
            (session.watchdog.now - session.watchdog.last_model_progress) * 1000
        )
        self.prepare_retry(session)
        session.watchdog.progress()
        yield session.watchdog.state(
            "retrying", self.retry_reason_code, elapsed_ms,
            phase=phases.ModelWaitPhase(), attempt=self,
        )


class AutoRetryStart(RetryAttemptEvent, DurablePiEvent, starts=phases.ProviderRetryPhase):
    retry_reason_code = "provider_auto_retry"
    recovery_on_progress = True

    def prepare_retry(self, session: TurnSession) -> None:
        session.output.final_assistant_stop = False
        session.watchdog.prompt_accepted = True
        session.watchdog.retry_recovery_pending = self.recovery_on_progress
        session.watchdog.retry_recovery_reason = "provider_auto_retry_progress"


class NativeRetryStart(AutoRetryStart):
    """The native turn recovery re-requests a refused empty response.

    stack/native-turn-recovery.mjs owns whether a provider refusal is retryable
    and how many times (RecoveryBudget); this wire kind carries that decision
    before the refused assistant message, so Core reports only the recovery's
    final outcome and never counts a refusal itself.
    """

    recovery_on_progress = False


class ReasonedCompaction(NativePiEvent, DurablePiEvent):
    """A compaction record owns decoding its external reason."""

    def __post_init__(self):
        if not isinstance(self.reason, type):
            object.__setattr__(self, "reason", CompactionReason.from_external(self.reason))

    @abstractmethod
    def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]: ...


@dataclass(frozen=True, kw_only=True)
class CompactionEnd(ReasonedCompaction, ends=phases.CompactionPhase):
    aborted: ReportedFlag | PiOmitted = PiOmitted()
    reason: type[CompactionReason] = UnknownCompactionReason
    result: ReportedCompaction | PiOmitted = PiOmitted()
    will_retry: Annotated[ReportedFlag | PiOmitted, WireName("willRetry")] = PiOmitted()
    error_message: Annotated[ReportedText | PiOmitted, WireName("errorMessage")] = PiOmitted()

    @property
    def completed(self) -> tuple[CompactionData, ...]:
        """The compaction Pi reported finishing: a result, and an explicit aborted=false."""
        return () if self.aborted.value_or(True) else self.result.present()

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.watchdog.progress()
        completed = self.completed
        for result in completed:
            for event in result.charge(session):
                yield event
            session.native.forget_context()
            session.watchdog.compacted(session.prompt_state.started)
        session.usage.invalidate()
        yield session.context_info()
        yield events.CompactionEnd(
            reason=self.reason,
            aborted=not completed,
            summary=next((result.display_summary for result in completed), None),
            will_retry=self.will_retry.value_or(False),
        )
        for message in self.error_message.present():
            yield session.output.error(message, source=UnrecordedFailureSource())
        if self.will_retry.value_or(False):
            session.request_retried()
            session.output.final_assistant_stop = False
            session.watchdog.retry_recovery_pending = True
            session.watchdog.retry_recovery_reason = "overflow_retry_progress"
            yield session.watchdog.state(
                "retrying", "overflow_compaction_retry", 0, phase=phases.ModelWaitPhase()
            )


@dataclass(frozen=True, kw_only=True)
class CompactionStart(ReasonedCompaction, starts=phases.CompactionPhase):
    def enter(self, member: type[phases.TurnPhase], current: phases.TurnPhase) -> phases.TurnPhase:
        return current.compacting()

    reason: type[CompactionReason] = UnknownCompactionReason

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.watchdog.progress()
        session.usage.invalidate()
        yield session.context_info()
        yield events.CompactionStart(reason=self.reason)


@dataclass(frozen=True, kw_only=True)
class ContextCommitted(NativePiEvent, NativeInputCommit):
    request_generation: Annotated[int | PiOmitted, WireName("requestGeneration")] = (
        PiOmitted()
    )
    llm_context_digest: Annotated[str | PiOmitted, WireName("llmContextDigest")] = (
        PiOmitted()
    )
    committed_at_ms: Annotated[int | PiOmitted, WireName("committedAtMs")] = PiOmitted()


@dataclass(frozen=True, kw_only=True)
class TurnContextObserved(NativePiEvent):
    context: NativeContextManifestData

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        for segment in self.context.segments:
            segment.remember_received(session.native)
        yield events.ContextObserved(self.context)


class ExtensionUiChoice(ABC):
    """The controller's decision, constrained to the requesting Pi dialog."""

    @abstractmethod
    def response(self, request: DialogUiRequest) -> ExtensionUiResponse: ...


@dataclass(frozen=True)
class CancelledUiChoice(ExtensionUiChoice):
    def response(self, request: DialogUiRequest) -> ExtensionUiResponse:
        return ExtensionUiResponse(id=request.id, cancelled=True)


@dataclass(frozen=True)
class ConfirmedUiChoice(ExtensionUiChoice):
    confirmed: bool

    def response(self, request: DialogUiRequest) -> ExtensionUiResponse:
        return request.confirm(self.confirmed)


@dataclass(frozen=True)
class ValueUiChoice(ExtensionUiChoice):
    value: str

    def response(self, request: DialogUiRequest) -> ExtensionUiResponse:
        return request.select(self.value)


@dataclass(frozen=True, kw_only=True)
class ExtensionUiRequest(NativePiEvent):
    """Pi owns the outer type; concrete declarations own its method vocabulary."""

    method: str | None = None
    id: PiRequestId = AnonymousRequest()

    @classmethod
    def wire_kind(cls) -> str:
        return ExtensionUiRequest.declared_name

    @classmethod
    def wire_case(cls, wire):
        method = wire.get("method")
        return next(
            (
                case
                for case in cls.members_with(cls)
                if case.method is not None and case.method == method
            ),
            cls,
        )

    def to_wire(self):
        data = super().to_wire()
        data.update(type="extension_ui_request", method=self.method)
        return data

    def require_id(self) -> str:
        identity = self.id.value_or("")
        if not 0 < len(identity) <= 128:
            raise ValueError("Pi extension UI request lacked a bounded ID.")
        return identity

    async def act(self, session: TurnSession) -> None:
        # Unknown methods never reach a controller or acquire reply authority.
        self.require_id()
        session.skip = True


@dataclass(frozen=True, kw_only=True)
class SetStatusUiRequest(ExtensionUiRequest):
    method: ClassVar[str] = "setStatus"
    status_key: Annotated[str | PiOmitted, WireName("statusKey")] = PiOmitted()
    status_text: Annotated[ReportedText | PiOmitted, WireName("statusText")] = PiOmitted()

    async def apply(self, session):
        if self.status_key == "pi-mcp/live-v1":
            acknowledgement = session.extension_ui.observe(self.status_text.value_or(None), session)
            if acknowledgement is not None:
                yield events.McpLiveStatus(acknowledgement=acknowledgement)
        session.skip = True


@dataclass(frozen=True, kw_only=True)
class DialogUiRequest(ExtensionUiRequest):
    title: str | None = None

    def confirm(self, value: bool) -> ExtensionUiResponse:
        return CancelledUiChoice().response(self)

    def select(self, value: str) -> ExtensionUiResponse:
        return CancelledUiChoice().response(self)

    def permission(self, turn_id: str):
        return None

    def choice(self, option_id: str) -> ExtensionUiChoice:
        return CancelledUiChoice()

    async def act(self, session: TurnSession) -> None:
        await session.extension_ui.answer(self, session)
        session.skip = True


class PermissionUiRequest(DialogUiRequest):
    @abstractmethod
    def permission_body(self) -> str: ...

    @abstractmethod
    def permission_options(self): ...

    def permission(self, turn_id: str):
        from acp.schema import ContentToolCallContent, TextContentBlock, ToolCallUpdate

        if self.title is None or not 0 < len(self.title) <= 160:
            return None
        body, options = self.permission_body(), self.permission_options()
        return (
            ToolCallUpdate(
                tool_call_id=f"pi-ui-{turn_id}-{self.id}",
                kind="other",
                title=self.title,
                content=[
                    ContentToolCallContent(
                        type="content",
                        content=TextContentBlock(type="text", text=body),
                    )
                ],
            ),
            options,
        )


@dataclass(frozen=True, kw_only=True)
class ConfirmUiRequest(PermissionUiRequest):
    method: ClassVar[str] = "confirm"
    message: str | None = None

    def confirm(self, value: bool) -> ExtensionUiResponse:
        return ExtensionUiResponse(id=self.id, confirmed=value)

    def permission_body(self) -> str:
        if self.message is None or len(self.message) > 8192:
            raise ValueError("Pi confirmation message is unavailable")
        return self.message

    def permission_options(self):
        from acp.schema import PermissionOption

        return [
            PermissionOption(option_id="allow-once", name="Allow once", kind="allow_once"),
            PermissionOption(option_id="deny", name="Deny", kind="reject_once"),
        ]

    def choice(self, option_id: str) -> ExtensionUiChoice:
        return ConfirmedUiChoice(option_id == "allow-once")


@dataclass(frozen=True, kw_only=True)
class SelectUiRequest(PermissionUiRequest):
    method: ClassVar[str] = "select"
    options: tuple[str, ...] | None = None

    def select(self, value: str) -> ExtensionUiResponse:
        if self.options and value in self.options:
            return ExtensionUiResponse(id=self.id, value=value)
        return CancelledUiChoice().response(self)

    def permission_body(self) -> str:
        return "Select one Pi extension option for this turn only."

    def choices(self) -> dict[str, str]:
        if self.options is None or not 1 <= len(self.options) <= 8:
            raise ValueError("Pi selection options are unavailable")
        if any(not item or len(item) > 100 for item in self.options):
            raise ValueError("Pi selection option is invalid")
        return {f"choice-{index}": value for index, value in enumerate(self.options)}

    def permission_options(self):
        from acp.schema import PermissionOption

        return [
            PermissionOption(option_id=key, name=f"Choose {value}", kind="allow_once")
            for key, value in self.choices().items()
        ] + [PermissionOption(option_id="deny", name="Cancel", kind="reject_once")]

    def choice(self, option_id: str) -> ExtensionUiChoice:
        value = self.choices().get(option_id)
        return CancelledUiChoice() if value is None else ValueUiChoice(value)


@dataclass(frozen=True, kw_only=True)
class InputUiRequest(DialogUiRequest):
    method: ClassVar[str] = "input"
    placeholder: str | None = None


@dataclass(frozen=True, kw_only=True)
class EditorUiRequest(DialogUiRequest):
    method: ClassVar[str] = "editor"
    default_value: Annotated[str | PiOmitted, WireName("defaultValue")] = PiOmitted()


@dataclass(frozen=True, kw_only=True)
class InputCommitted(NativePiEvent, NativeInputLocation):
    pass


@dataclass(frozen=True, kw_only=True)
class MessageEnd(phases.AcceptsPrompt, NativePiEvent, DurablePiEvent):
    message: PiMessage = field(
        default_factory=AbsentMessage
    )

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        for event in await self.message.apply_end(session):
            yield event

    @property
    def retry_progress(self) -> bool:
        return self.message.retry_progress


@dataclass(frozen=True, kw_only=True)
class MessageStart(phases.AcceptsPrompt, NativePiEvent, DurablePiEvent):
    message: PiMessage = field(
        default_factory=AbsentMessage
    )

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        for update in await self.message.apply_start(session, self):
            yield update

    @property
    def invalidates_stop(self) -> bool:
        return self.message.invalidates_start

    @property
    def retry_progress(self) -> bool:
        return self.message.assistant

    def observe_abort(self, session: TurnSession) -> None:
        self.message.observe_start_abort(session, self)


@dataclass(frozen=True, kw_only=True)
class MessageUpdate(phases.AcceptsPrompt, NativePiEvent, DurablePiEvent):
    assistant_message_event: Annotated[
        NoDelta | PiDelta, WireName("assistantMessageEvent")
    ] = NoDelta()
    message: PiMessage = field(
        default_factory=AbsentMessage
    )
    usage: Annotated[PiUsage, WireOmitDefault()] = UnreportedUsage()

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        if self.message.update_context and not session.native.attestation.uncertain:
            tokens = self.message.measured_tokens.otherwise(self.usage.total_tokens)
            for event in tokens.provisional(session):
                yield event

        for event in await self.assistant_message_event.emit(session):
            yield event

    def convert(self):
        return self.assistant_message_event.events()

    @property
    def retry_progress(self) -> bool:
        return self.delta_progress

    @property
    def delta_progress(self) -> bool:
        return self.assistant_message_event.progress


@dataclass(frozen=True, kw_only=True)
class Response(NativePiEvent, DurablePiEvent):
    command: type[PiCommand] = UnknownCommand
    data: PiResponseData = field(default_factory=MissingData)
    error: ReportedText | PiOmitted = PiOmitted()
    id: PiRequestId = AnonymousRequest()
    success: ReportedFlag | PiOmitted = PiOmitted()

    @classmethod
    def decode_fields(cls, value, decoded):
        command = FieldCodec.field_plan(cls, "command")
        owner = (PiCommand.response_owner(value[command.key])
                 if command.key in value else cls.command)
        owner.validate_response(value, cls)
        return super().decode_fields(value, dict(
            decoded, command=owner,
            data=owner.decode_response_data(value.get("data")),
        ))

    @classmethod
    def outcome(cls, record: Mapping[str, object]) -> type[ResponseOutcome]:
        return ResponseOutcome.from_record(record)

    @property
    def succeeded(self) -> bool:
        """Pi reported success; an omitted success is not one."""
        return self.success.value_or(False)

    @property
    def refused(self) -> bool:
        """Pi reported failure outright, as opposed to leaving success out."""
        return not self.success.value_or(True)

    async def deliver_to(self, channel) -> None:
        channel.correlate(self)

    def responds_to(self, request: PiCommand) -> bool:
        return self.id == request.id and self.command is type(request)

    def rejection_details(self) -> dict:
        """Private diagnostic projection, excluding response data and prompt content."""
        return {
            "command": self.command.declared_name,
            "id": FieldCodec.encode(self.id),
            "success": FieldCodec.encode(self.success),
            "error": FieldCodec.encode(self.error),
        }

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        owner = session.native.reader.correlate(self)
        if owner.invalidates_identity(self, session):
            for event in await session.invalidate_identity():
                yield event
            return
        for event in owner.on_response(self, session):
            yield event


class ResponseOutcome(DeclaredFamily, affix="ResponseOutcome"):
    """Each declared outcome owns its selected payload and validation."""

    success_value: ClassVar[bool]
    payload_field: ClassVar[FieldPlan]

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> type[ResponseOutcome]:
        success = FieldCodec.decode(bool, record.get("success"))
        member, = (member for member in cls.members_with(cls)
                   if member.success_value == success)
        return member

    @classmethod
    def excluded_fields(cls) -> set[str]:
        keys = {member.payload_field.key
                for member in ResponseOutcome.members_with(ResponseOutcome)}
        return keys - {cls.payload_field.key}

    @classmethod
    def validate_payload(cls, record: Mapping[str, object]) -> None:
        pass


class SucceededResponseOutcome(ResponseOutcome):
    success_value = True
    payload_field = FieldCodec.field_plan(Response, "data")


class FailedResponseOutcome(ResponseOutcome):
    success_value = False
    payload_field = FieldCodec.field_plan(Response, "error")

    @classmethod
    def validate_payload(cls, record: Mapping[str, object]) -> None:
        FieldCodec.decode(str, record[cls.payload_field.key])


class SteeringInterruptCompleted(NativePiEvent):
    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.explicit_interrupt = False
        session.output.interrupted()
        yield events.SteeringInterrupted()


class SteeringInterruptStarted(NativePiEvent):
    async def act(self, session: TurnSession) -> None:
        session.explicit_interrupt = True


class SummarizationRetryAttemptStart(RetryAttemptEvent, starts=phases.SummarizationRetryPhase):
    retry_reason_code = "summarization_retry"


@dataclass(frozen=True, kw_only=True)
class SummarizationRetryFinished(NativePiEvent, ends=phases.SummarizationRetryPhase):
    result: ReportedCompaction | PiOmitted = PiOmitted()
    success: ReportedFlag | PiOmitted = PiOmitted()

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.watchdog.progress()
        if self.success.value_or(False) or self.result.reported:
            yield session.watchdog.state(
                "recovered", "summarization_retry_succeeded", 0, phase=phases.ModelWaitPhase()
            )


class SummarizationRetryScheduled(RetryAttemptEvent, starts=phases.SummarizationRetryPhase):
    retry_reason_code = "summarization_retry"


class PiToolCallEvent:
    """Pi's tool events name their call; an omitted or empty name reads as "tool"."""

    tool_call_id: ReportedText | PiOmitted
    tool_name: ReportedText | PiOmitted

    @property
    def tool_label(self) -> str:
        return self.tool_name.text_or("tool")

    @property
    def tool_id(self) -> str:
        return self.tool_call_id.text_or(self.tool_label)


@dataclass(frozen=True, kw_only=True)
class ToolExecutionEnd(
    phases.AcceptsPrompt,
    PiToolCallEvent,
    NativePiEvent,
    DurablePiEvent,
    ends=phases.ToolRunningPhase,
):
    def leave(self, current: phases.TurnPhase) -> phases.TurnPhase:
        return current.tool_ended(self.tool_id)

    is_error: Annotated[ReportedFlag | PiOmitted, WireName("isError")] = PiOmitted()
    result: PiToolResult = field(default_factory=MissingToolResult)
    tool_call_id: Annotated[ReportedText | PiOmitted, WireName("toolCallId")] = PiOmitted()
    tool_name: Annotated[ReportedText | PiOmitted, WireName("toolName")] = PiOmitted()

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        name = self.tool_label
        is_ok = not self.is_error.value_or(False)
        tool_id = self.tool_id
        session.watchdog.tick()
        session.watchdog.progress()
        yield events.ToolEnd(
            id=tool_id,
            name=name,
            ok=is_ok,
            output=self.result.text(),
            diff=ToolDeclaration.for_name(name).result_diff(self.result, is_ok),
            sent_message=self.result.sent_message(is_ok),
        )


@dataclass(frozen=True, kw_only=True)
class ToolExecutionStart(
    phases.AcceptsPrompt,
    RetryProgress,
    PiToolCallEvent,
    NativePiEvent,
    DurablePiEvent,
    starts=phases.ToolRunningPhase,
):
    def enter(self, member: type[phases.TurnPhase], current: phases.TurnPhase) -> phases.TurnPhase:
        return current.tool_started(self.tool_id, self.tool_label)

    args: object = field(default_factory=dict)
    tool_call_id: Annotated[ReportedText | PiOmitted, WireName("toolCallId")] = PiOmitted()
    tool_name: Annotated[ReportedText | PiOmitted, WireName("toolName")] = PiOmitted()

    def convert(self):
        return (events.ToolStart.from_call(self.tool_id, self.tool_label, self.args),)

    async def act(self, session: TurnSession) -> None:
        session.watchdog.prompt_accepted = True


@dataclass(frozen=True, kw_only=True)
class ToolExecutionUpdate(phases.AcceptsPrompt, PiToolCallEvent, NativePiEvent, DurablePiEvent):

    partial_result: Annotated[PiToolResult, WireName("partialResult")] = field(
        default_factory=MissingToolResult
    )
    tool_call_id: Annotated[ReportedText | PiOmitted, WireName("toolCallId")] = PiOmitted()
    tool_name: Annotated[ReportedText | PiOmitted, WireName("toolName")] = PiOmitted()

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:

        yield events.ToolProgress(
            id=self.tool_id,
            name=self.tool_label,
            output=self.partial_result.text(),
        )


from .pi_native_backend import PiNativeBackend  # noqa: E402

PiNativeBackend.require_event_family(NativePiEvent)
