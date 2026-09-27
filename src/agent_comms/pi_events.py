"""Pi's open event vocabulary, decoded by the RPC channel before execution."""

from __future__ import annotations
import asyncio
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any
from . import agent_events as events
from . import turn_failure as failures
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec

if TYPE_CHECKING:
    from .backend import TurnSession


@dataclass(frozen=True)
class PiEvent(DeclaredFamily, Mapping[str, Any]):
    wire: dict[str, Any]

    @classmethod
    def from_wire(cls, wire: dict[str, Any]) -> PiEvent:
        try:
            member = cls.decode(wire.get("type"))
        except ValueError:
            member = UnknownPiEvent
        data = {"kind": member.declared_name, "wire": wire}
        for declared in fields(member):
            key = declared.metadata.get("wire_name", declared.name)
            if declared.name != "wire" and key in wire:
                data[key] = wire[key]
        return FieldCodec.decode(cls, data)

    def __getitem__(self, key: str) -> Any:
        return self.wire[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.wire)

    def __len__(self) -> int:
        return len(self.wire)

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


class UnknownPiEvent(PiEvent):
    """Unrecognized events remain ignorable, never admission evidence."""


@dataclass(frozen=True, kw_only=True)
class AgentEnd(PiEvent):
    will_retry: Any = field(default=None, metadata={"wire_name": "willRetry"})
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
        session.agent_settled_seen = True
        if not session.stats.requested:
            session.last_model_progress = session.loop.time()
            session.phase = phases.SettlingStatsPhase()
            if session.persistent_session is not None:
                await session.stats.request(session)
            else:
                yield events.StreamSettled()
            if session.persistent_session is None and session.finish_event is None:
                await session.stats.request(session)
        if False:
            yield

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
    success: Any = field(default=None, metadata={"wire_name": "success"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        from . import turn_phase as phases

        session.last_model_progress = session.now
        session.phase = phases.ModelWaitPhase()
        if self.success:
            session.error_message = None
        else:
            session.retry_recovery_pending = False
            session.error_message = "Provider retry attempts were exhausted."
            yield session.turn_state(
                "failed", "provider_retry_exhausted", 0, event_phase="model_wait"
            )
        if False:
            yield


@dataclass(frozen=True, kw_only=True)
class AutoRetryStart(PiEvent):
    attempt: Any = field(default=None, metadata={"wire_name": "attempt"})
    max_attempts: Any = field(default=None, metadata={"wire_name": "maxAttempts"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.final_assistant_stop = False
        session.prompt_accepted = True
        session.retry_recovery_pending = True
        session.retry_recovery_reason = "provider_auto_retry_progress"
        session.elapsed_ms = round((session.now - session.last_model_progress) * 1000)
        session.last_model_progress = session.now
        session.current = self.attempt if isinstance(self.attempt, int) else None
        session.maximum = self.max_attempts if isinstance(self.max_attempts, int) else None
        yield session.turn_state(
            "retrying",
            "provider_auto_retry",
            session.elapsed_ms,
            event_phase="model_wait",
            attempt=(session.current, session.maximum),
        )
        if False:
            yield


@dataclass(frozen=True, kw_only=True)
class CompactionEnd(PiEvent):
    aborted: Any = field(default=None, metadata={"wire_name": "aborted"})
    reason: Any = field(default=None, metadata={"wire_name": "reason"})
    result: Any = field(default=None, metadata={"wire_name": "result"})
    will_retry: Any = field(default=None, metadata={"wire_name": "willRetry"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        from . import turn_phase as phases
        from .backend import PROMPT_START_TIMEOUT_SECONDS, _terminate_process, compaction_summary

        session.last_model_progress = session.now
        session.phase = phases.ModelWaitPhase()
        session.result = self.result
        session.completed = self.aborted is False and isinstance(session.result, dict)
        if (
            session.completed
            and (not session.usage.compaction_recorded)
            and isinstance(session.result.get("usage"), dict)
        ):
            yield session.usage.charge(session.result["usage"])
        if (
            session.completed
            and (not session.initial_input_started)
            and (session.prompt_start_deadline is not None)
        ):
            session.prompt_start_deadline = session.now + PROMPT_START_TIMEOUT_SECONDS
        session.usage.invalidate()
        yield session.context_info()
        session.reason = self.reason
        session.summary = session.result.get("summary") if session.completed else None
        yield events.CompactionEnd(
            reason=session.reason
            if session.reason in {"manual", "threshold", "overflow"}
            else "unknown",
            aborted=not session.completed,
            summary=compaction_summary(session.summary)
            if isinstance(session.summary, str)
            else None,
            context_used=None,
            will_retry=self.will_retry is True,
        )
        if not session.completed and (not session.initial_input_started):
            session.record_failure(
                failures.PrestartCompactionFailed(
                    "Context compaction failed before this input started; inspect ACP diagnostics."
                )
            )
            yield session.turn_state(
                "failed", "prestart_compaction_failed", 0, event_phase="compaction"
            )
            await _terminate_process(session.proc)
            session.finished = True
            return
        if self.will_retry:
            session.final_assistant_stop = False
            session.retry_recovery_pending = True
            session.retry_recovery_reason = "overflow_retry_progress"
            yield session.turn_state(
                "retrying", "overflow_compaction_retry", 0, event_phase="model_wait"
            )
        if False:
            yield


@dataclass(frozen=True, kw_only=True)
class CompactionProgress(PiEvent):
    chunk_index: Any = field(default=None, metadata={"wire_name": "chunkIndex"})
    source_bytes_done: Any = field(default=None, metadata={"wire_name": "sourceBytesDone"})
    source_bytes_total: Any = field(default=None, metadata={"wire_name": "sourceBytesTotal"})
    summary_phase: Any = field(default=None, metadata={"wire_name": "summaryPhase"})
    usage: Any = field(default=None, metadata={"wire_name": "usage"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.last_model_progress = session.now
        session.chunk_index = self.chunk_index
        session.provider_usage = self.usage
        if isinstance(session.provider_usage, dict):
            session.usage.response_index += 1
            session.usage.compaction_recorded = True
            yield events.ProviderUsage(
                response_id=str(session.usage.response_index), usage=session.provider_usage
            )
        session.done = self.source_bytes_done
        session.total = self.source_bytes_total
        session.measured = (
            type(session.done) is int
            and type(session.total) is int
            and (0 <= session.done <= session.total)
            and (session.total > 0)
        )
        if type(session.chunk_index) is int and (
            session.chunk_index > 0 or (session.chunk_index == 0 and session.measured)
        ):
            yield events.CompactionProgress(
                chunk_index=session.chunk_index,
                source_bytes_done=session.done if session.measured else None,
                source_bytes_total=session.total if session.measured else None,
                summary_phase=self.summary_phase
                if isinstance(self.summary_phase, str) and self.summary_phase
                else None,
            )
        if False:
            yield


@dataclass(frozen=True, kw_only=True)
class CompactionStart(PiEvent):
    reason: Any = field(default=None, metadata={"wire_name": "reason"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.compaction_started = True
        session.usage.compaction_recorded = False
        session.last_model_progress = session.now
        session.usage.invalidate()
        yield session.context_info()
        session.reason = self.reason
        yield events.CompactionStart(
            reason=session.reason
            if session.reason in {"manual", "threshold", "overflow"}
            else "unknown"
        )
        if False:
            yield


@dataclass(frozen=True, kw_only=True)
class ContextCommitted(PiEvent):
    input_id: Any = field(default=None, metadata={"wire_name": "inputId"})
    pass


class ExtensionUiRequest(PiEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class InputCommitted(PiEvent):
    input_id: Any = field(default=None, metadata={"wire_name": "inputId"})
    pass


@dataclass(frozen=True, kw_only=True)
class MessageEnd(PiEvent):
    message: Any = field(default=None, metadata={"wire_name": "message"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.message = self.message or {}
        if session.message.get("role") == "assistant":
            session.content = session.message.get("content")
            session.committed_text = (
                "".join(
                    (
                        part["text"]
                        for part in session.content
                        if isinstance(part, dict)
                        and part.get("type") == "text"
                        and (type(part.get("text")) is str)
                    )
                )
                if isinstance(session.content, list)
                else ""
            )
            if (
                session.message.get("stopReason") == "toolUse"
                and session.initial_prompt_acknowledged
                and session.initial_input_started
                and (not session.inputs.uncertain)
                and (not session.session_identity_uncertain)
                and session.committed_text
                and (session.committed_text == "".join(session.assistant_message_parts))
            ):
                yield events.CommittedProgress(text=session.committed_text)
            session.assistant_message_parts.clear()
            session.provider_usage = session.message.get("usage")
            if isinstance(session.provider_usage, dict) and (
                not session.session_identity_uncertain
            ):
                yield session.usage.charge(session.provider_usage)
            session.stop_reason = session.message.get("stopReason")
            session.final_assistant_stop = (
                session.stop_reason == "stop"
                and session.initial_input_started
                and (not session.inputs.uncertain)
            )
            if session.stop_reason in {"error", "aborted"}:
                if session.usage.provisional:
                    session.usage.used = session.usage.confirmed
                    session.usage.provisional = False
                    yield session.context_info()
                session.error_message = (
                    "Image prompt failed; backend diagnostics withheld."
                    if session.image_input_sent or session.inherited_image_sensitive
                    else str(session.message.get("errorMessage") or "").strip()
                    or f"Model request {session.stop_reason}"
                )
                if not (session.explicit_interrupt and session.stop_reason == "aborted"):
                    yield events.Error(text=session.error_message)
            else:
                session.error_message = None
                session.tokens = session.usage.positive_tokens(session.message.get("usage"))
                if session.tokens is not None and (not session.session_identity_uncertain):
                    session.usage.used = session.tokens
                    session.usage.confirmed = session.tokens
                    yield session.context_info()
                elif session.usage.provisional:
                    session.usage.used = session.usage.confirmed
                    yield session.context_info()
                session.usage.provisional = False
        if False:
            yield

    accepts_prompt = True
    output_progress = True

    @property
    def retry_progress(self) -> bool:
        return (self.message or {}).get("role") == "assistant" and (self.message or {}).get(
            "stopReason"
        ) not in {"error", "aborted"}


@dataclass(frozen=True, kw_only=True)
class MessageStart(PiEvent):
    message: Any = field(default=None, metadata={"wire_name": "message"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        from .backend import _ACTIVE_STEERING

        if (self.message or {}).get("role") == "assistant":
            session.assistant_message_parts.clear()
        elif (self.message or {}).get("role") == "user":
            session.message = self.message
            session.content = session.message.get("content")
            if isinstance(session.content, str):
                session.user_text = session.content
            elif isinstance(session.content, list):
                session.user_text = "\n".join(
                    (
                        part.get("text", "")
                        for part in session.content
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                )
            else:
                session.user_text = None
            session.native_id = session.message.get("inputId")
            if (
                session.initial_prompt_acknowledged
                and (not session.initial_input_started)
                and (not session.inputs.uncertain)
                and (session.user_text == session.task)
                and (not session.require_input_id or session.native_id == session.original_input_id)
            ):
                if session.native_start is not None and (
                    not session.native_start(None, session.original_input_id, session.task)
                ):
                    session.inputs.uncertain = True
                    session.record_failure(
                        failures.InputMissing("Pi input start did not match the durable attempt.")
                    )
                    await session.abort_stalled_rpc()
                    session.finished = True
                    return
                session.initial_input_started = True
                if session.native_start is not None:
                    yield events.InputStarted(id=None)
                if (
                    session.steering_queue is not None
                    and session.proc.stdin is not None
                    and (session.steering_task is None)
                ):
                    session.steering_task = asyncio.create_task(session.inputs.forward(session))
                    if session.owner is not None:
                        _ACTIVE_STEERING[session.owner] = session.steering_task
            elif session.initial_input_started and (not session.inputs.uncertain):
                session.matched, session.input_id = session.inputs.mark_started(session, self)
                if session.matched:
                    yield events.InputStarted(id=session.input_id)
                else:
                    session.inputs.uncertain = True
                    session.final_assistant_stop = False
                    session.record_failure(
                        failures.FollowupUnrecognized(
                            "Pi RPC saw an unrecognized follow-up user message start."
                        )
                    )
                    await session.abort_stalled_rpc()
                    session.finished = True
                    return
            else:
                session.inputs.uncertain = True
                session.final_assistant_stop = False
                session.record_failure(
                    failures.InputMissing(
                        "Pi RPC run ended without this prompt's user message start."
                    )
                )
                await session.abort_stalled_rpc()
                session.finished = True
                return
        if False:
            yield

    accepts_prompt = True
    output_progress = True

    @property
    def invalidates_stop(self) -> bool:
        return (self.message or {}).get("role") in {"user", "assistant"}

    @property
    def retry_progress(self) -> bool:
        return (self.message or {}).get("role") == "assistant"


@dataclass(frozen=True, kw_only=True)
class MessageUpdate(PiEvent):
    assistant_message_event: Any = field(
        default=None, metadata={"wire_name": "assistantMessageEvent"}
    )
    message: Any = field(default=None, metadata={"wire_name": "message"})
    usage: Any = field(default=None, metadata={"wire_name": "usage"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.message = self.message or {}
        if not isinstance(session.message, dict):
            session.message = {}
        if session.message.get("role", "assistant") == "assistant":
            session.tokens = session.usage.positive_tokens(
                session.message.get("usage")
            ) or session.usage.positive_tokens(self.usage)
            if session.tokens is not None and (not session.session_identity_uncertain):
                session.usage.used = session.tokens
                session.usage.provisional = True
                yield session.context_info()
        session.delta_event = self.assistant_message_event or {}
        session.delta_type = session.delta_event.get("type")
        if session.delta_type == "text_delta":
            session.piece = session.delta_event.get("delta") or ""
            if session.piece:
                session.output_started = True
            session.text_parts.append(session.piece)
            session.assistant_message_parts.append(session.piece)
            yield events.Chunk(text=session.piece)
        elif session.delta_type == "thinking_delta":
            session.piece = session.delta_event.get("delta") or ""
            if session.piece:
                session.output_started = True
                yield events.Thinking(text=session.piece)
        elif session.delta_type in {"toolcall_start", "toolcall_delta", "toolcall_end"}:
            session.output_started = True
        if False:
            yield

    accepts_prompt = True
    output_progress = True

    @property
    def retry_progress(self) -> bool:
        return self.delta_progress

    @property
    def delta_progress(self) -> bool:
        delta = self.assistant_message_event or {}
        return delta.get("type") in {
            "text_delta",
            "thinking_delta",
            "toolcall_start",
            "toolcall_delta",
            "toolcall_end",
        } and bool(delta.get("delta") or delta.get("type", "").startswith("toolcall"))


@dataclass(frozen=True, kw_only=True)
class Response(PiEvent):
    command: Any = field(default=None, metadata={"wire_name": "command"})
    data: Any = field(default=None, metadata={"wire_name": "data"})
    error: Any = field(default=None, metadata={"wire_name": "error"})
    id: Any = field(default=None, metadata={"wire_name": "id"})
    success: Any = field(default=None, metadata={"wire_name": "success"})

    @property
    def command_type(self):
        from .pi_commands import PiCommand

        return PiCommand.response_owner(self.command)

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        async for event in self.command_type.on_response(self, session):
            yield event


class SteeringInterruptCompleted(PiEvent):
    pass


class SteeringInterruptStarted(PiEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class SummarizationRetryAttemptStart(PiEvent):
    attempt: Any = field(default=None, metadata={"wire_name": "attempt"})
    max_attempts: Any = field(default=None, metadata={"wire_name": "maxAttempts"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.compaction_started = True
        session.elapsed_ms = round((session.now - session.last_model_progress) * 1000)
        session.last_model_progress = session.now
        session.current = self.attempt if isinstance(self.attempt, int) else None
        session.maximum = self.max_attempts if isinstance(self.max_attempts, int) else None
        yield session.turn_state(
            "retrying",
            "summarization_retry",
            session.elapsed_ms,
            event_phase="model_wait",
            attempt=(session.current, session.maximum),
        )
        if False:
            yield


@dataclass(frozen=True, kw_only=True)
class SummarizationRetryFinished(PiEvent):
    result: Any = field(default=None, metadata={"wire_name": "result"})
    success: Any = field(default=None, metadata={"wire_name": "success"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        from . import turn_phase as phases

        session.last_model_progress = session.now
        session.phase = phases.ModelWaitPhase()
        if self.success or self.result:
            yield session.turn_state(
                "recovered", "summarization_retry_succeeded", 0, event_phase="model_wait"
            )
        if False:
            yield


@dataclass(frozen=True, kw_only=True)
class SummarizationRetryScheduled(PiEvent):
    attempt: Any = field(default=None, metadata={"wire_name": "attempt"})
    max_attempts: Any = field(default=None, metadata={"wire_name": "maxAttempts"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        session.compaction_started = True
        session.elapsed_ms = round((session.now - session.last_model_progress) * 1000)
        session.last_model_progress = session.now
        session.current = self.attempt if isinstance(self.attempt, int) else None
        session.maximum = self.max_attempts if isinstance(self.max_attempts, int) else None
        yield session.turn_state(
            "retrying",
            "summarization_retry",
            session.elapsed_ms,
            event_phase="model_wait",
            attempt=(session.current, session.maximum),
        )
        if False:
            yield


@dataclass(frozen=True, kw_only=True)
class ToolExecutionEnd(PiEvent):
    is_error: Any = field(default=None, metadata={"wire_name": "isError"})
    result: Any = field(default=None, metadata={"wire_name": "result"})
    tool_call_id: Any = field(default=None, metadata={"wire_name": "toolCallId"})
    tool_name: Any = field(default=None, metadata={"wire_name": "toolName"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        from . import turn_phase as phases
        from .backend import _result_text
        from .tool_results import ToolDiff

        session.name = self.tool_name or "tool"
        session.result = self.result or {}
        session.output = _result_text(session.result)
        session.is_ok = self.is_error is not True
        session.tool_id = self.tool_call_id or session.name
        session.active_tools.discard(session.tool_id)
        session.last_model_progress = session.loop.time()
        if not session.active_tools:
            session.phase = phases.ModelWaitPhase()
        yield events.ToolEnd(
            id=session.tool_id,
            name=session.name,
            ok=session.is_ok,
            output=session.output,
            diff=ToolDiff.from_result(session.name, session.result, session.is_ok),
        )
        if False:
            yield


@dataclass(frozen=True, kw_only=True)
class ToolExecutionStart(PiEvent):
    args: Any = field(default=None, metadata={"wire_name": "args"})
    tool_call_id: Any = field(default=None, metadata={"wire_name": "toolCallId"})
    tool_name: Any = field(default=None, metadata={"wire_name": "toolName"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        from .backend import _tool_title

        session.name = self.tool_name or "tool"
        session.args = self.args or {}
        session.tool_id = self.tool_call_id or session.name
        session.prompt_accepted = True
        session.tool_ever_started = True
        session.active_tools.add(session.tool_id)
        yield events.ToolStart(
            id=session.tool_id,
            name=session.name,
            title=_tool_title(session.name, session.args),
            args=session.args,
        )
        if False:
            yield

    tool_progress = True

    @property
    def retry_progress(self) -> bool:
        return True


@dataclass(frozen=True, kw_only=True)
class ToolExecutionUpdate(PiEvent):
    partial_result: Any = field(default=None, metadata={"wire_name": "partialResult"})
    tool_call_id: Any = field(default=None, metadata={"wire_name": "toolCallId"})
    tool_name: Any = field(default=None, metadata={"wire_name": "toolName"})

    async def apply(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        from .backend import _result_text

        yield events.ToolProgress(
            id=self.tool_call_id or self.tool_name or "tool",
            name=self.tool_name or "tool",
            output=_result_text(self.partial_result),
        )
        if False:
            yield
