"""Pi's open event vocabulary, decoded by the RPC channel before execution."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import agent_events as events
from . import turn_failure as failures
from .declared_family import DeclaredFamily
from .pi_commands import ExtensionUiResponse, PiCommand, UnknownCommand
from .pi_payloads import (
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
    def wire_member(cls, wire):
        try:
            return cls.decode(wire.get("type"))
        except ValueError:
            return UnknownPiEvent

    async def consume(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        """Apply shared progress and phase behavior around this event's meaning."""
        async for update in session.watchdog.observe(self, session):
            yield update
        async for event in self.apply(session):
            yield event
        session.watchdog.transition(self, session.active_tools)

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
    pass
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
    pass
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


@dataclass(frozen=True, kw_only=True)
class CompactionEnd(PiEvent):
    aborted: bool | None = field(default=None, metadata={"wire_name": "aborted"})
    reason: str | None = field(default=None, metadata={"wire_name": "reason"})
    result: CompactionData | None = field(default=None, metadata={"wire_name": "result"})
    will_retry: bool | None = field(default=None, metadata={"wire_name": "willRetry"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        from .backend import compaction_summary

        session.watchdog.progress()
        session.result = self.result
        session.completed = self.aborted is False and session.result is not None
        if (
            session.completed
            and (not session.usage.compaction_recorded)
            and session.result.usage is not None
        ):
            yield session.usage.charge(session.result.usage)
        if session.completed:
            session.watchdog.compacted(session.admission.started)
        session.usage.invalidate()
        yield session.context_info()
        session.reason = self.reason
        session.summary = session.result.summary if session.completed else None
        yield events.CompactionEnd(
            reason=(
                session.reason
                if session.reason in {"manual", "threshold", "overflow"}
                else "unknown"
            ),
            aborted=not session.completed,
            summary=compaction_summary(session.summary) if session.summary is not None else None,
            context_used=None,
            will_retry=self.will_retry is True,
        )
        if not session.completed and (not session.admission.started):
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
    chunk_index: int | None = field(default=None, metadata={"wire_name": "chunkIndex"})
    source_bytes_done: int | None = field(default=None, metadata={"wire_name": "sourceBytesDone"})
    source_bytes_total: int | None = field(default=None, metadata={"wire_name": "sourceBytesTotal"})
    summary_phase: str | None = field(default=None, metadata={"wire_name": "summaryPhase"})
    usage: PiUsage | None = field(default=None, metadata={"wire_name": "usage"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.watchdog.progress()
        session.chunk_index = self.chunk_index
        session.provider_usage = self.usage
        if session.provider_usage is not None:
            session.usage.response_index += 1
            session.usage.compaction_recorded = True
            yield events.ProviderUsage(
                response_id=str(session.usage.response_index), usage=session.provider_usage
            )
        session.done = self.source_bytes_done
        session.total = self.source_bytes_total
        session.measured = (
            session.done is not None
            and session.total is not None
            and (0 <= session.done <= session.total)
            and (session.total > 0)
        )
        if session.chunk_index is not None and (
            session.chunk_index > 0 or (session.chunk_index == 0 and session.measured)
        ):
            yield events.CompactionProgress(
                chunk_index=session.chunk_index,
                source_bytes_done=session.done if session.measured else None,
                source_bytes_total=session.total if session.measured else None,
                summary_phase=self.summary_phase if self.summary_phase else None,
            )


@dataclass(frozen=True, kw_only=True)
class CompactionStart(PiEvent):
    reason: str | None = field(default=None, metadata={"wire_name": "reason"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.watchdog.compaction_started = True
        session.usage.compaction_recorded = False
        session.watchdog.progress()
        session.usage.invalidate()
        yield session.context_info()
        session.reason = self.reason
        yield events.CompactionStart(
            reason=(
                session.reason
                if session.reason in {"manual", "threshold", "overflow"}
                else "unknown"
            )
        )

    def observe_abort(self, session: TurnSession) -> None:
        session.watchdog.compaction_started = True


@dataclass(frozen=True, kw_only=True)
class ContextCommitted(PiEvent):
    input_id: str | None = field(default=None, metadata={"wire_name": "inputId"})
    pass
    session_id: str | None = field(default=None, metadata={"wire_name": "sessionId"})
    session_entry_id: str | None = field(default=None, metadata={"wire_name": "sessionEntryId"})
    request_generation: int | None = field(
        default=None, metadata={"wire_name": "requestGeneration"}
    )
    llm_context_digest: str | None = field(default=None, metadata={"wire_name": "llmContextDigest"})


class ExtensionUiChoice(ABC):
    """The controller's decision, constrained to the requesting Pi dialog."""

    @abstractmethod
    def response(self, request: ExtensionUiRequest) -> ExtensionUiResponse: ...


@dataclass(frozen=True)
class CancelledUiChoice(ExtensionUiChoice):
    def response(self, request: ExtensionUiRequest) -> ExtensionUiResponse:
        return ExtensionUiResponse(id=request.id, cancelled=True)


@dataclass(frozen=True)
class ConfirmedUiChoice(ExtensionUiChoice):
    confirmed: bool

    def response(self, request: ExtensionUiRequest) -> ExtensionUiResponse:
        if request.method == "confirm":
            return ExtensionUiResponse(id=request.id, confirmed=self.confirmed)
        return CancelledUiChoice().response(request)


@dataclass(frozen=True)
class ValueUiChoice(ExtensionUiChoice):
    value: str

    def response(self, request: ExtensionUiRequest) -> ExtensionUiResponse:
        if request.method == "select" and request.options and self.value in request.options:
            return ExtensionUiResponse(id=request.id, value=self.value)
        return CancelledUiChoice().response(request)


@dataclass(frozen=True, kw_only=True)
class ExtensionUiRequest(PiEvent):
    method: str | None = None
    id: str | None = None
    status_key: str | None = field(default=None, metadata={"wire_name": "statusKey"})
    status_text: str | None = field(default=None, metadata={"wire_name": "statusText"})
    title: str | None = None
    message: str | None = None
    options: tuple[str, ...] | None = None
    placeholder: str | None = None
    default_value: str | None = field(default=None, metadata={"wire_name": "defaultValue"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        from .backend import _pi_mcp_live_receipt

        if self.method == "setStatus":
            if (
                not session.live_status_seen
                and (not session.admission.settled)
                and (not session.stats.requested)
                and session.require_input_id
                and session.admission.started
                and session.native.attestation.identity is not None
                and (not session.inputs.uncertain)
            ):
                session.receipt = _pi_mcp_live_receipt(self, session.original_input_id)
                if session.receipt is not None:
                    session.live_status_seen = True
                    yield events.McpLiveStatus(receipt=session.receipt)
            session.skip = True
            return
        session.request_id = self.id
        session.method = self.method
        if session.request_id is None or not session.request_id or len(session.request_id) > 128:
            session.output.record_failure(
                failures.ExtensionUiFailed("Pi extension UI request lacked a bounded ID.")
            )
            await session.native.proc.stop()
            session.finished = True
            return
        if session.method not in {"confirm", "select", "input", "editor"}:
            session.skip = True
            return
        choice: ExtensionUiChoice = CancelledUiChoice()
        if (
            session.request_id not in session.ui_seen
            and len(session.ui_seen) < 64
            and (session.ui_request is not None)
            and session.admission.started
            and session.native.attestation.identity is not None
            and (not session.inputs.uncertain)
        ):
            session.ui_seen.add(session.request_id)
            with suppress(Exception):
                choice = await asyncio.wait_for(session.ui_request(self), timeout=15)
        response = choice.response(self)
        try:
            if session.native.proc.stdin is None or session.native.proc.returncode is not None:
                raise BrokenPipeError
            session.native.proc.stdin.write(session.native.reader.encode(response))
            await asyncio.wait_for(session.native.proc.stdin.drain(), timeout=2)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            session.output.record_failure(
                failures.ExtensionUiFailed(
                    "Pi extension UI response could not reach the requesting child."
                )
            )
            await session.native.proc.stop()
            session.finished = True
            return
        session.skip = True
        return


@dataclass(frozen=True, kw_only=True)
class InputCommitted(PiEvent):
    input_id: str | None = field(default=None, metadata={"wire_name": "inputId"})
    pass
    session_id: str | None = field(default=None, metadata={"wire_name": "sessionId"})
    session_entry_id: str | None = field(default=None, metadata={"wire_name": "sessionEntryId"})


@dataclass(frozen=True, kw_only=True)
class MessageEnd(PiEvent):
    message: PiMessage | None = field(default=None, metadata={"wire_name": "message"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.message = self.message
        if session.message is not None and session.message.assistant:
            session.committed_text = session.message.text
            if (
                session.message.stop_reason == "toolUse"
                and session.admission.started
                and (not session.inputs.uncertain)
                and (not session.native.attestation.uncertain)
                and session.output.matches_message(session.committed_text)
            ):
                yield events.CommittedProgress(text=session.committed_text)
            session.output.start_message()
            session.provider_usage = session.message.usage
            if session.provider_usage is not None and (not session.native.attestation.uncertain):
                yield session.usage.charge(session.provider_usage)
            session.stop_reason = session.message.stop_reason
            session.output.final_assistant_stop = (
                session.stop_reason == "stop"
                and session.admission.started
                and (not session.inputs.uncertain)
            )
            if session.stop_reason in {"error", "aborted"}:
                if session.usage.provisional:
                    session.usage.used = session.usage.confirmed
                    session.usage.provisional = False
                    yield session.context_info()
                error = session.output.error(
                    str(session.message.error_message or "").strip()
                    or f"Model request {session.stop_reason}",
                    session.message.diagnostics,
                )
                if not (session.explicit_interrupt and session.stop_reason == "aborted"):
                    yield error
            else:
                session.output.error_message = None
                session.tokens = (
                    session.message.usage.positive_tokens
                    if session.message.usage is not None
                    else None
                )
                if session.tokens is not None and (not session.native.attestation.uncertain):
                    session.usage.used = session.tokens
                    session.usage.confirmed = session.tokens
                    yield session.context_info()
                elif session.usage.provisional:
                    session.usage.used = session.usage.confirmed
                    yield session.context_info()
                session.usage.provisional = False

    accepts_prompt = True
    output_progress = True

    @property
    def retry_progress(self) -> bool:
        return (
            self.message is not None
            and self.message.assistant
            and self.message.stop_reason not in {"error", "aborted"}
        )


@dataclass(frozen=True, kw_only=True)
class MessageStart(PiEvent):
    message: PiMessage | None = field(default=None, metadata={"wire_name": "message"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        if self.message is not None and self.message.assistant:
            session.output.start_message()
        elif self.message is not None and self.message.user:
            session.message = self.message
            session.user_text = session.message.text
            session.native_id = session.message.input_id
            if (
                session.admission.awaiting_start
                and (not session.inputs.uncertain)
                and (session.user_text == session.task)
                and (not session.require_input_id or session.native_id == session.original_input_id)
            ):
                if session.native_start is not None and (
                    not session.native_start(None, session.original_input_id, session.task)
                ):
                    session.inputs.uncertain = True
                    session.output.record_failure(
                        failures.InputMissing("Pi input start did not match the durable attempt.")
                    )
                    await session.abort_stalled_rpc()
                    session.finished = True
                    return
                session.admission = session.admission.start(self)
                if session.native_start is not None:
                    yield events.InputStarted(id=None)
                if (
                    session.inputs.queue is not None
                    and session.native.proc.stdin is not None
                    and (session.steering_task is None)
                ):
                    session.steering_task = asyncio.create_task(session.inputs.forward(session))
            elif session.admission.started and (not session.inputs.uncertain):
                session.matched, session.input_id = session.inputs.mark_started(session, self)
                if session.matched:
                    yield events.InputStarted(id=session.input_id)
                else:
                    session.inputs.uncertain = True
                    session.output.final_assistant_stop = False
                    session.output.record_failure(
                        failures.FollowupUnrecognized(
                            "Pi RPC saw an unrecognized follow-up user message start."
                        )
                    )
                    await session.abort_stalled_rpc()
                    session.finished = True
                    return
            else:
                session.inputs.uncertain = True
                session.output.final_assistant_stop = False
                session.output.record_failure(
                    failures.InputMissing(
                        "Pi RPC run ended without this prompt's user message start."
                    )
                )
                await session.abort_stalled_rpc()
                session.finished = True
                return

    accepts_prompt = True
    output_progress = True

    @property
    def invalidates_stop(self) -> bool:
        return self.message is not None and (self.message.user or self.message.assistant)

    @property
    def retry_progress(self) -> bool:
        return self.message is not None and self.message.assistant

    def observe_abort(self, session: TurnSession) -> None:
        if not session.native.attestation.uncertain:
            matched, identity = session.inputs.mark_started(session, self)
            if matched:
                session.started_during_abort.append(identity)


@dataclass(frozen=True, kw_only=True)
class MessageUpdate(PiEvent):
    assistant_message_event: PiDelta | None = field(
        default=None, metadata={"wire_name": "assistantMessageEvent"}
    )
    message: PiMessage | None = field(default=None, metadata={"wire_name": "message"})
    usage: PiUsage | None = field(default=None, metadata={"wire_name": "usage"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.message = self.message
        if session.message is None or session.message.assistant:
            message_usage = session.message.usage if session.message is not None else None
            session.tokens = (
                message_usage.positive_tokens if message_usage is not None else None
            ) or (self.usage.positive_tokens if self.usage is not None else None)
            if session.tokens is not None and (not session.native.attestation.uncertain):
                session.usage.used = session.tokens
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
    args: dict[str, Any] | None = field(default=None, metadata={"wire_name": "args"})
    tool_call_id: str | None = field(default=None, metadata={"wire_name": "toolCallId"})
    tool_name: str | None = field(default=None, metadata={"wire_name": "toolName"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        from .backend import _tool_title

        session.name = self.tool_name or "tool"
        session.args = self.args or {}
        session.tool_id = self.tool_call_id or session.name
        session.watchdog.prompt_accepted = True
        session.watchdog.tool_ever_started = True
        session.active_tools.add(session.tool_id)
        yield events.ToolStart(
            id=session.tool_id,
            name=session.name,
            title=_tool_title(session.name, session.args),
            args=session.args,
        )

    tool_progress = True

    @property
    def retry_progress(self) -> bool:
        return True

    def observe_abort(self, session: TurnSession) -> None:
        session.watchdog.tool_ever_started = True


@dataclass(frozen=True, kw_only=True)
class ToolExecutionUpdate(PiEvent):
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

    def __post_init__(self):
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("Positive selected compaction progress sequence required")
