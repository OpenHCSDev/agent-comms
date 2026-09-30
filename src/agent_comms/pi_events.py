"""Pi's open event vocabulary, decoded by the RPC channel before execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from . import agent_events as events
from . import turn_failure as failures
from .declared_family import DeclaredFamily
from .compaction_progress import CompactionSourceProgress
from .pi_vocabulary import CompactionReason, UnknownCompactionReason
from .pi_commands import ExtensionUiResponse, PiCommand, UnknownCommand
from .pi_payloads import (
    AbsentMessage,
    CompactionData,
    PiDelta,
    PiMessage,
    PiPayload,
    PiResponseData,
    PiToolResult,
    PiUsage,
)

if TYPE_CHECKING:
    from .backend import TurnSession


@dataclass(frozen=True)
class PiEvent(PiPayload, DeclaredFamily):
    wire_tag = "type"
    opaque = False

    @classmethod
    def normalize_field(cls, target, key, value, record):
        if key == "message" and target is PiMessage and value is None:
            return {"kind": AbsentMessage.declared_name}
        if key == "reason" and target == type[CompactionReason]:
            return CompactionReason.from_external(value).declared_name
        return super().normalize_field(target, key, value, record)

    @classmethod
    def wire_member(cls, wire):
        try:
            return cls.decode(wire.get("type")).wire_case(wire)
        except ValueError:
            return UnknownPiEvent

    @classmethod
    def wire_case(cls, wire):
        return cls

    async def consume(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        """Apply shared progress and phase behavior around this event's meaning."""
        async for event in session.consume_native_event(self):
            yield event

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        if False:
            yield

    accepts_prompt = False
    output_progress = False
    tool_progress = False

    @property
    def invalidates_stop(self) -> bool:
        return False

    @property
    def retry_progress(self) -> bool:
        return False

    def observe_abort(self, session: TurnSession) -> None:
        pass


@dataclass(frozen=True)
class UnknownPiEvent(PiEvent):
    """Unrecognized events remain ignorable, never admission evidence."""

    payload: dict[str, Any]
    opaque = True


@dataclass(frozen=True, kw_only=True)
class AgentEnd(PiEvent):
    will_retry: bool | None = field(default=None, metadata={"wire_name": "willRetry"})
    accepts_prompt = True

    @property
    def invalidates_stop(self) -> bool:
        return self.will_retry is True


class AgentSettled(PiEvent):
    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        from . import turn_phase as phases

        session.settlement_count += 1
        if session.persistent_session is not None and session.inputs.pending:
            session.skip = True
            return
        session.admission = session.admission.settle(self)
        if not session.stats.requested:
            session.watchdog.tick()
            session.watchdog.progress()
            session.watchdog.phase = phases.SettlingStatsPhase()
            if session.persistent_session is not None:
                await session.stats.request(session)
            else:
                yield events.StreamSettled()
            if session.persistent_session is None and session.finish_event is None:
                await session.stats.request(session)

    @property
    def retry_progress(self) -> bool:
        return True


class AgentStart(PiEvent):
    accepts_prompt = True

    @property
    def invalidates_stop(self) -> bool:
        return True


@dataclass(frozen=True, kw_only=True)
class AutoRetryEnd(PiEvent):
    success: bool | None = field(default=None, metadata={"wire_name": "success"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.watchdog.progress()
        if self.success:
            session.output.error_message = None
        else:
            session.watchdog.retry_recovery_pending = False
            session.output.error_message = "Provider retry attempts were exhausted."
            yield session.watchdog.state(
                session, "failed", "provider_retry_exhausted", 0, event_phase="model_wait"
            )


@dataclass(frozen=True, kw_only=True)
class AutoRetryStart(PiEvent):
    attempt: int | None = field(default=None, metadata={"wire_name": "attempt"})
    max_attempts: int | None = field(default=None, metadata={"wire_name": "maxAttempts"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.output.final_assistant_stop = False
        session.watchdog.prompt_accepted = True
        session.watchdog.retry_recovery_pending = True
        session.watchdog.retry_recovery_reason = "provider_auto_retry_progress"
        session.elapsed_ms = round(
            (session.watchdog.now - session.watchdog.last_model_progress) * 1000
        )
        session.watchdog.progress()
        session.current = self.attempt
        session.maximum = self.max_attempts
        yield session.watchdog.state(
            session,
            "retrying",
            "provider_auto_retry",
            session.elapsed_ms,
            event_phase="model_wait",
            attempt=(session.current, session.maximum),
        )


class ReasonedCompaction(PiEvent):
    """A compaction record owns decoding its external reason."""

    def __post_init__(self):
        if not isinstance(self.reason, type):
            object.__setattr__(self, "reason", CompactionReason.from_external(self.reason))

    @abstractmethod
    async def apply(self, session):
        if False:
            yield


@dataclass(frozen=True, kw_only=True)
class CompactionEnd(ReasonedCompaction):
    aborted: bool | None = field(default=None, metadata={"wire_name": "aborted"})
    reason: type[CompactionReason] = field(
        default=UnknownCompactionReason, metadata={"wire_name": "reason"}
    )
    result: CompactionData | None = field(default=None, metadata={"wire_name": "result"})
    will_retry: bool | None = field(default=None, metadata={"wire_name": "willRetry"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.watchdog.progress()
        completed = self.aborted is False and self.result is not None
        if completed:
            for event in self.result.charge(session):
                yield event
        if completed:
            session.watchdog.compacted(session.admission.started)
        session.usage.invalidate()
        yield session.context_info()
        yield events.CompactionEnd(
            reason=self.reason.declared_name,
            aborted=not completed,
            summary=self.result.display_summary if completed else None,
            context_used=None,
            will_retry=self.will_retry is True,
        )
        if not completed and (not session.admission.started):
            session.output.record_failure(
                failures.PrestartCompactionFailed(
                    "Context compaction failed before this input started; inspect ACP diagnostics."
                )
            )
            yield session.watchdog.state(
                session, "failed", "prestart_compaction_failed", 0, event_phase="compaction"
            )
            await session.native.proc.stop()
            session.finished = True
            return
        if self.will_retry:
            session.output.final_assistant_stop = False
            session.watchdog.retry_recovery_pending = True
            session.watchdog.retry_recovery_reason = "overflow_retry_progress"
            yield session.watchdog.state(
                session, "retrying", "overflow_compaction_retry", 0, event_phase="model_wait"
            )


@dataclass(frozen=True, kw_only=True)
class CompactionProgress(PiEvent):
    operation_id: str = field(metadata={"wire_name": "operationId"})
    chunk_index: int = field(metadata={"wire_name": "chunkIndex"})
    reason: type[CompactionReason] = field(default=UnknownCompactionReason)
    text: str = ""
    source: CompactionSourceProgress | None = None
    usage: PiUsage | None = field(default=None, metadata={"wire_name": "usage"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.watchdog.progress()
        if self.usage is not None:
            session.usage.response_index += 1
            session.usage.compaction_recorded = True
            yield events.ProviderUsage(
                response_id=str(session.usage.response_index), usage=self.usage
            )
        yield events.CompactionProgress(
            reason=self.reason.declared_name,
            operation_id=self.operation_id,
            text=self.text,
            chunk_index=self.chunk_index,
            source=self.source,
        )


@dataclass(frozen=True, kw_only=True)
class CompactionStart(ReasonedCompaction):
    reason: type[CompactionReason] = field(
        default=UnknownCompactionReason, metadata={"wire_name": "reason"}
    )

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.watchdog.compaction_started = True
        session.usage.compaction_recorded = False
        session.watchdog.progress()
        session.usage.invalidate()
        yield session.context_info()
        yield events.CompactionStart(reason=self.reason.declared_name)

    def observe_abort(self, session: TurnSession) -> None:
        session.watchdog.compaction_started = True


@dataclass(frozen=True, kw_only=True)
class ContextCommitted(PiEvent):
    input_id: str | None = field(default=None, metadata={"wire_name": "inputId"})
    session_id: str | None = field(default=None, metadata={"wire_name": "sessionId"})
    session_entry_id: str | None = field(default=None, metadata={"wire_name": "sessionEntryId"})
    request_generation: int | None = field(
        default=None, metadata={"wire_name": "requestGeneration"}
    )
    llm_context_digest: str | None = field(default=None, metadata={"wire_name": "llmContextDigest"})


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
class ExtensionUiRequest(PiEvent):
    """Pi owns the outer type; concrete declarations own its method vocabulary."""

    method: str | None = None
    id: str | None = None

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
        if self.id is None or not 0 < len(self.id) <= 128:
            raise ValueError("Pi extension UI request lacked a bounded ID.")
        return self.id

    async def apply(self, session):
        # Unknown methods never reach a controller or acquire reply authority.
        try:
            self.require_id()
        except ValueError as error:
            await session.extension_ui.fail(str(error), session)
        session.skip = True
        if False:
            yield


@dataclass(frozen=True, kw_only=True)
class SetStatusUiRequest(ExtensionUiRequest):
    method: ClassVar[str] = "setStatus"
    status_key: str | None = field(default=None, metadata={"wire_name": "statusKey"})
    status_text: str | None = field(default=None, metadata={"wire_name": "statusText"})

    async def apply(self, session):
        if self.status_key == "pi-mcp/live-v1":
            receipt = session.extension_ui.observe(self.status_text, session)
            if receipt is not None:
                yield events.McpLiveStatus(receipt=receipt)
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

    async def apply(self, session):
        await session.extension_ui.answer(self, session)
        session.skip = True
        if False:
            yield


class PermissionUiRequest(DialogUiRequest):
    @abstractmethod
    def permission_body(self) -> str: ...

    @abstractmethod
    def permission_options(self): ...

    def permission(self, turn_id: str):
        from acp.schema import ContentToolCallContent, TextContentBlock, ToolCallUpdate

        if self.title is None or not 0 < len(self.title) <= 160:
            return None
        try:
            body, options = self.permission_body(), self.permission_options()
        except ValueError:
            return None
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
    default_value: str | None = field(default=None, metadata={"wire_name": "defaultValue"})


@dataclass(frozen=True, kw_only=True)
class InputCommitted(PiEvent):
    input_id: str | None = field(default=None, metadata={"wire_name": "inputId"})
    session_id: str | None = field(default=None, metadata={"wire_name": "sessionId"})
    session_entry_id: str | None = field(default=None, metadata={"wire_name": "sessionEntryId"})


@dataclass(frozen=True, kw_only=True)
class MessageEnd(PiEvent):
    message: PiMessage = field(default_factory=AbsentMessage, metadata={"wire_name": "message"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        async for event in self.message.apply_end(session):
            yield event

    accepts_prompt = True
    output_progress = True

    @property
    def retry_progress(self) -> bool:
        return self.message.retry_progress


@dataclass(frozen=True, kw_only=True)
class MessageStart(PiEvent):
    message: PiMessage = field(default_factory=AbsentMessage, metadata={"wire_name": "message"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        async for update in self.message.apply_start(session, self):
            yield update

    accepts_prompt = True
    output_progress = True

    @property
    def invalidates_stop(self) -> bool:
        return self.message.invalidates_start

    @property
    def retry_progress(self) -> bool:
        return self.message.assistant

    def observe_abort(self, session: TurnSession) -> None:
        self.message.observe_start_abort(session, self)


@dataclass(frozen=True, kw_only=True)
class MessageUpdate(PiEvent):
    assistant_message_event: PiDelta | None = field(
        default=None, metadata={"wire_name": "assistantMessageEvent"}
    )
    message: PiMessage = field(default_factory=AbsentMessage, metadata={"wire_name": "message"})
    usage: PiUsage | None = field(default=None, metadata={"wire_name": "usage"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        if self.message.update_context:
            tokens = self.message.measured_tokens or (
                self.usage.positive_tokens if self.usage is not None else None
            )
            if tokens is not None and not session.native.attestation.uncertain:
                session.usage.used = tokens
                session.usage.provisional = True
                yield session.context_info()

        if self.assistant_message_event is not None:
            for event in self.assistant_message_event.emit(session):
                yield event

    accepts_prompt = True
    output_progress = True

    @property
    def retry_progress(self) -> bool:
        return self.delta_progress

    @property
    def delta_progress(self) -> bool:
        return self.assistant_message_event is not None and self.assistant_message_event.progress

    def observe_abort(self, session: TurnSession) -> None:
        if self.delta_progress:
            session.watchdog.output_started = True


@dataclass(frozen=True, kw_only=True)
class Response(PiEvent):
    command: type[PiCommand] = UnknownCommand
    data: PiResponseData | None = None
    error: str | None = field(default=None, metadata={"wire_name": "error"})
    id: str | None = field(default=None, metadata={"wire_name": "id"})
    success: bool | None = field(default=None, metadata={"wire_name": "success"})

    async def consume(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        if self.command.invalidates_identity(self, session):
            async for event in session.invalidate_identity():
                yield event
            return
        async for event in super().consume(session):
            yield event

    def rejection_details(self) -> dict:
        """Private diagnostic projection, excluding response data and prompt content."""
        return {
            "command": self.command.declared_name,
            "id": self.id,
            "success": self.success,
            "error": self.error,
        }

    @classmethod
    def normalize_field(cls, target, key, value, record):
        owner = PiCommand.response_owner(record.get("command"))
        if owner.strict_response and set(record) != {"id", "type", "command", "success", "data"}:
            raise ValueError("Unexpected selected response envelope")
        if key == "command":
            return owner.declared_name
        if key == "data" and value is not None:
            return owner.response_payload.normalize_wire(value)
        return super().normalize_field(target, key, value, record)

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        owner = session.native.reader.correlate(self) or self.command
        async for event in owner.on_response(self, session):
            yield event


class SteeringInterruptCompleted(PiEvent):
    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.explicit_interrupt = False
        session.output.interrupted()
        yield events.SteeringInterrupted()


class SteeringInterruptStarted(PiEvent):
    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.explicit_interrupt = True
        if False:
            yield


@dataclass(frozen=True, kw_only=True)
class SummarizationRetryAttemptStart(PiEvent):
    attempt: int | None = field(default=None, metadata={"wire_name": "attempt"})
    max_attempts: int | None = field(default=None, metadata={"wire_name": "maxAttempts"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.watchdog.compaction_started = True
        session.elapsed_ms = round(
            (session.watchdog.now - session.watchdog.last_model_progress) * 1000
        )
        session.watchdog.progress()
        session.current = self.attempt
        session.maximum = self.max_attempts
        yield session.watchdog.state(
            session,
            "retrying",
            "summarization_retry",
            session.elapsed_ms,
            event_phase="model_wait",
            attempt=(session.current, session.maximum),
        )

    def observe_abort(self, session: TurnSession) -> None:
        session.watchdog.compaction_started = True


@dataclass(frozen=True, kw_only=True)
class SummarizationRetryFinished(PiEvent):
    result: CompactionData | None = field(default=None, metadata={"wire_name": "result"})
    success: bool | None = field(default=None, metadata={"wire_name": "success"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.watchdog.progress()
        if self.success or self.result:
            yield session.watchdog.state(
                session, "recovered", "summarization_retry_succeeded", 0, event_phase="model_wait"
            )


@dataclass(frozen=True, kw_only=True)
class SummarizationRetryScheduled(PiEvent):
    attempt: int | None = field(default=None, metadata={"wire_name": "attempt"})
    max_attempts: int | None = field(default=None, metadata={"wire_name": "maxAttempts"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.watchdog.compaction_started = True
        session.elapsed_ms = round(
            (session.watchdog.now - session.watchdog.last_model_progress) * 1000
        )
        session.watchdog.progress()
        session.current = self.attempt
        session.maximum = self.max_attempts
        yield session.watchdog.state(
            session,
            "retrying",
            "summarization_retry",
            session.elapsed_ms,
            event_phase="model_wait",
            attempt=(session.current, session.maximum),
        )

    def observe_abort(self, session: TurnSession) -> None:
        session.watchdog.compaction_started = True


@dataclass(frozen=True, kw_only=True)
class ToolExecutionEnd(PiEvent):
    accepts_prompt = True

    is_error: bool | None = field(default=None, metadata={"wire_name": "isError"})
    result: PiToolResult | None = field(default=None, metadata={"wire_name": "result"})
    tool_call_id: str | None = field(default=None, metadata={"wire_name": "toolCallId"})
    tool_name: str | None = field(default=None, metadata={"wire_name": "toolName"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        from .tool_results import ToolDiff

        name = self.tool_name or "tool"
        is_ok = self.is_error is not True
        tool_id = self.tool_call_id or name
        session.active_tools.discard(tool_id)
        session.watchdog.tick()
        session.watchdog.progress()
        yield events.ToolEnd(
            id=tool_id,
            name=name,
            ok=is_ok,
            output=self.result.text() if self.result is not None else "",
            diff=ToolDiff.from_result(name, self.result, is_ok),
        )


@dataclass(frozen=True, kw_only=True)
class ToolExecutionStart(PiEvent):
    accepts_prompt = True

    args: dict[str, Any] | None = field(default=None, metadata={"wire_name": "args"})
    tool_call_id: str | None = field(default=None, metadata={"wire_name": "toolCallId"})
    tool_name: str | None = field(default=None, metadata={"wire_name": "toolName"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        from .native_tools import NativeTool

        name = self.tool_name or "tool"
        tool_id = self.tool_call_id or name
        session.watchdog.prompt_accepted = True
        session.watchdog.tool_ever_started = True
        session.active_tools.add(tool_id)
        yield NativeTool.start(tool_id, name, self.args or {})

    tool_progress = True

    @property
    def retry_progress(self) -> bool:
        return True

    def observe_abort(self, session: TurnSession) -> None:
        session.watchdog.tool_ever_started = True


@dataclass(frozen=True, kw_only=True)
class ToolExecutionUpdate(PiEvent):
    accepts_prompt = True

    partial_result: PiToolResult | None = field(
        default=None, metadata={"wire_name": "partialResult"}
    )
    tool_call_id: str | None = field(default=None, metadata={"wire_name": "toolCallId"})
    tool_name: str | None = field(default=None, metadata={"wire_name": "toolName"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:

        yield events.ToolProgress(
            id=self.tool_call_id or self.tool_name or "tool",
            name=self.tool_name or "tool",
            output=self.partial_result.text() if self.partial_result is not None else "",
        )

    def observe_abort(self, session: TurnSession) -> None:
        session.watchdog.tool_ever_started = True


@dataclass(frozen=True, kw_only=True)
class AgentCommsCompactionProgress(PiEvent):
    """Actual selected compaction progress, scoped to exactly one RPC attempt."""

    id: str
    operation_id: str = field(metadata={"wire_name": "operationId"})
    sequence: int
    text: str
    source: CompactionSourceProgress | None

    def __post_init__(self):
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("Positive selected compaction progress sequence required")
