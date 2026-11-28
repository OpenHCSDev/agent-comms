"""External transcript publication for Antigravity ACP turns.

``AntigravitySession`` is an ``ExternalTranscriptLocation``: Core's transcript pager
(``comms_transcript_page``) and live watchers (``ExternalTranscriptsView``) read a
thread's thoughts, tool calls, tool results, and in-progress streaming drafts from
``ExternalTranscript``. Publishing every streamed ACP event here keeps Android,
desktop reopen, ``comms_turns``, and ``last_tool_call`` in sync with live execution.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from agent_comms import agent_events as events
from agent_comms.agent_backend import TurnTranscriptSink
from agent_comms.cli_bridges.tool_calls import CliToolArguments, ToolCall
from agent_comms.coordination.work import CoordinationWork
from agent_comms.execution.transcript_sources import ExternalTranscript
from agent_comms.execution.turn_item import NativeItemId, NativeTurnId
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import ToolCallId, TurnId
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.transcript.transcript_events import (
    AssistantTranscript,
    ThinkingTranscript,
)

_DRAFT_FLUSH_INTERVAL_SECONDS = 0.25
_JSON_DICT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def _tool_arguments(raw_input: object) -> dict[str, JsonValue]:
    if isinstance(raw_input, dict):
        return _JSON_DICT.validate_python({str(key): value for key, value in raw_input.items()})
    if raw_input in (None, ""):
        return {}
    return {"input": _JSON_VALUE.validate_python(raw_input)}


class _StreamedTextBuffer(ABC):
    """Nominal state for a streamed thinking or assistant text item."""

    @property
    @abstractmethod
    def has_content(self) -> bool: ...

    @abstractmethod
    def append(self, text: str) -> _ActiveTextBuffer: ...

    @abstractmethod
    def item_and_timestamp(self) -> tuple[NativeItemId, float]: ...

    @abstractmethod
    def flush_with(self, emit: Callable[[NativeItemId, float, str], None]) -> _IdleTextBuffer: ...


class _IdleTextBuffer(_StreamedTextBuffer):
    """No streamed text item is currently open."""

    @property
    def has_content(self) -> bool:
        return False

    def append(self, text: str) -> _ActiveTextBuffer:
        return _ActiveTextBuffer(
            item_id=NativeItemId.fresh(),
            started_at=time.time(),
            parts=[text],
        )

    def item_and_timestamp(self) -> tuple[NativeItemId, float]:
        return NativeItemId.fresh(), time.time()

    def flush_with(self, emit: Callable[[NativeItemId, float, str], None]) -> _IdleTextBuffer:
        del emit
        return self


@dataclass
class _ActiveTextBuffer(_StreamedTextBuffer):
    """Accumulated chunks for one in-progress thinking or assistant item."""

    item_id: NativeItemId
    started_at: float
    parts: list[str]

    @property
    def has_content(self) -> bool:
        return bool(self.parts)

    @property
    def text(self) -> str:
        return "".join(self.parts)

    def append(self, text: str) -> _ActiveTextBuffer:
        self.parts.append(text)
        return self

    def item_and_timestamp(self) -> tuple[NativeItemId, float]:
        return self.item_id, self.started_at

    def flush_with(self, emit: Callable[[NativeItemId, float, str], None]) -> _IdleTextBuffer:
        body = self.text
        if body.strip():
            emit(self.item_id, self.started_at, body)
        return _IDLE_BUFFER


_IDLE_BUFFER = _IdleTextBuffer()


@dataclass
class ExternalTurnTranscriptSink(TurnTranscriptSink):
    """Writes one Antigravity ACP turn's live drafts and items to ``ExternalTranscript``."""

    transcript: ExternalTranscript
    turn_id: NativeTurnId
    opening: tuple[MessageReference, ...]
    _opened: bool = False
    _thinking: _StreamedTextBuffer = _IDLE_BUFFER
    _reply: _StreamedTextBuffer = _IDLE_BUFFER
    _last_draft_at: float = 0.0
    _tool_calls: dict[ToolCallId, ToolCall] = field(default_factory=dict)

    @classmethod
    def for_turn(
        cls,
        root: Path,
        incarnation: ThreadIncarnation,
        turn_id: TurnId,
        opening: tuple[MessageReference, ...],
    ) -> ExternalTurnTranscriptSink:
        return cls(
            transcript=ExternalTranscript(root, incarnation),
            turn_id=NativeTurnId(str(turn_id)),
            opening=opening,
        )

    def _ensure_opened(self) -> None:
        if self._opened:
            return
        self._opened = True
        if self.opening:
            self.transcript.delivered(self.turn_id, self.opening)

    def _emit_thinking(self, item_id: NativeItemId, started_at: float, text: str) -> None:
        self._ensure_opened()
        self.transcript.finish(
            turn_id=self.turn_id,
            item_id=item_id,
            events=(ThinkingTranscript(text, position=item_id, timestamp=started_at),),
        )

    def _flush_thinking(self) -> None:
        self._thinking = self._thinking.flush_with(self._emit_thinking)

    def _publish_reply_draft(self, item_id: NativeItemId, started_at: float, body: str) -> None:
        self._ensure_opened()
        self.transcript.draft(
            turn_id=self.turn_id,
            item_id=item_id,
            events=(AssistantTranscript(body, position=item_id, timestamp=started_at),),
        )

    def _record_tool_start(self, event: events.ToolStart) -> None:
        self._flush_thinking()
        self._ensure_opened()
        arguments = _tool_arguments(event.args)
        call = CliToolArguments.decode(
            ToolCall(id=event.id, name=event.name, arguments=arguments),
            arguments,
        )
        self._tool_calls[event.id] = call
        item_id = NativeItemId(f"acp-tool-start-{event.id}")
        start_event = call.started(item_id, time.time())
        self.transcript.finish(
            turn_id=self.turn_id,
            item_id=item_id,
            events=(start_event,),
        )

    def _record_tool_end(self, event: events.ToolEnd) -> None:
        self._flush_thinking()
        self._ensure_opened()
        call = self._tool_calls.pop(
            event.id,
            ToolCall(id=event.id, name=event.name, arguments={}),
        )
        item_id = NativeItemId(f"acp-tool-end-{event.id}")
        end_event = call.ended(event.output or "", event.ok, item_id, time.time())
        self.transcript.finish(
            turn_id=self.turn_id,
            item_id=item_id,
            events=(end_event,),
        )

    async def on_thinking(self, text: str) -> None:
        if not text:
            return
        self._thinking = self._thinking.append(text)

    async def on_chunk(self, text: str) -> None:
        if not text:
            return
        if self._thinking.has_content:
            await CoordinationWork.run_worker(self._flush_thinking)
        active = self._reply.append(text)
        self._reply = active
        now = time.monotonic()
        due = (
            self._last_draft_at == 0.0 or now - self._last_draft_at >= _DRAFT_FLUSH_INTERVAL_SECONDS
        )
        body = active.text
        if due and body.strip():
            self._last_draft_at = now
            await CoordinationWork.run_worker(
                partial(self._publish_reply_draft, active.item_id, active.started_at, body)
            )

    async def on_tool_start(self, event: events.ToolStart) -> None:
        await CoordinationWork.run_worker(partial(self._record_tool_start, event))

    async def on_tool_end(self, event: events.ToolEnd) -> None:
        await CoordinationWork.run_worker(partial(self._record_tool_end, event))

    def _emit_assistant(
        self,
        item_id: NativeItemId,
        started_at: float,
        body: str,
        published: tuple[MessageReference, ...],
    ) -> None:
        self._ensure_opened()
        event = AssistantTranscript(body, position=item_id, timestamp=started_at)
        if published:
            event = event.with_publications(published)
        self.transcript.finish(
            turn_id=self.turn_id,
            item_id=item_id,
            events=(event,),
        )

    def on_committed_progress(
        self,
        text: str,
        published: tuple[MessageReference, ...],
    ) -> None:
        self._flush_thinking()
        item_id, started_at = self._reply.item_and_timestamp()
        self._reply = _IDLE_BUFFER
        self._last_draft_at = 0.0
        if text.strip():
            self._emit_assistant(item_id, started_at, text, published)

    def discard_reply(self) -> None:
        self._flush_thinking()
        if not self._reply.has_content:
            return
        item_id, _ = self._reply.item_and_timestamp()
        self._reply = _IDLE_BUFFER
        self._last_draft_at = 0.0
        if self._opened:
            self.transcript.finish(
                turn_id=self.turn_id,
                item_id=item_id,
                events=(),
            )

    def on_finish(self, published: tuple[MessageReference, ...]) -> None:
        self._flush_thinking()
        self._reply = self._reply.flush_with(
            lambda item_id, started_at, body: self._emit_assistant(
                item_id, started_at, body, published
            )
        )
        self._last_draft_at = 0.0
        if self._opened:
            self.transcript.finish_replies(self.turn_id)
            self.transcript.end_turn()

    def on_abort(self) -> None:
        self._flush_thinking()
        self._reply = _IDLE_BUFFER
        self._last_draft_at = 0.0
        if self._opened:
            self.transcript.finish_replies(self.turn_id)
            self.transcript.end_turn()
