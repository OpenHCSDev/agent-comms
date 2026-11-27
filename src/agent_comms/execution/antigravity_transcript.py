"""External transcript publication for Antigravity ACP turns.

``AntigravitySession`` is an ``ExternalTranscriptLocation``: Core's transcript pager
(``comms_transcript_page``) and live watchers (``ExternalTranscriptsView``) read a
thread's thoughts, tool calls, tool results, and in-progress streaming drafts from
``ExternalTranscript``. Publishing every streamed ACP event here keeps Android,
desktop reopen, ``comms_turns``, and ``last_tool_call`` in sync with live execution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from pydantic import JsonValue

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


def _tool_arguments(raw_input: object) -> dict[str, JsonValue]:
    if isinstance(raw_input, dict):
        return {str(key): value for key, value in raw_input.items()}
    if raw_input in (None, ""):
        return {}
    return {"input": raw_input}


@dataclass
class ExternalTurnTranscriptSink(TurnTranscriptSink):
    """Writes one Antigravity ACP turn's live drafts and completed items to ``ExternalTranscript``."""

    transcript: ExternalTranscript
    turn_id: NativeTurnId
    opening: tuple[MessageReference, ...]
    _opened: bool = False
    _thinking_item_id: NativeItemId | None = None
    _thinking_started_at: float = 0.0
    _thinking_parts: list[str] = field(default_factory=list)
    _reply_item_id: NativeItemId | None = None
    _reply_started_at: float = 0.0
    _reply_parts: list[str] = field(default_factory=list)
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

    def _flush_thinking(self) -> None:
        if not self._thinking_parts or self._thinking_item_id is None:
            return
        text = "".join(self._thinking_parts)
        item_id = self._thinking_item_id
        started_at = self._thinking_started_at
        self._thinking_item_id = None
        self._thinking_parts.clear()
        if not text.strip():
            return
        self._ensure_opened()
        self.transcript.finish(
            turn_id=self.turn_id,
            item_id=item_id,
            events=(ThinkingTranscript(text, position=item_id, timestamp=started_at),),
        )

    def _publish_reply_draft(self) -> None:
        if not self._reply_parts or self._reply_item_id is None:
            return
        body = "".join(self._reply_parts)
        if not body.strip():
            return
        self._ensure_opened()
        self.transcript.draft(
            turn_id=self.turn_id,
            item_id=self._reply_item_id,
            events=(
                AssistantTranscript(
                    body,
                    position=self._reply_item_id,
                    timestamp=self._reply_started_at,
                ),
            ),
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
        if self._thinking_item_id is None:
            self._thinking_item_id = NativeItemId.fresh()
            self._thinking_started_at = time.time()
        self._thinking_parts.append(text)

    async def on_chunk(self, text: str) -> None:
        if not text:
            return
        if self._thinking_parts:
            await CoordinationWork.run_worker(self._flush_thinking)
        if self._reply_item_id is None:
            self._reply_item_id = NativeItemId.fresh()
            self._reply_started_at = time.time()
            self._last_draft_at = 0.0
        self._reply_parts.append(text)
        now = time.monotonic()
        if (
            self._last_draft_at == 0.0
            or now - self._last_draft_at >= _DRAFT_FLUSH_INTERVAL_SECONDS
        ):
            if "".join(self._reply_parts).strip():
                self._last_draft_at = now
                await CoordinationWork.run_worker(self._publish_reply_draft)

    async def on_tool_start(self, event: events.ToolStart) -> None:
        await CoordinationWork.run_worker(partial(self._record_tool_start, event))

    async def on_tool_end(self, event: events.ToolEnd) -> None:
        await CoordinationWork.run_worker(partial(self._record_tool_end, event))

    def on_committed_progress(
        self,
        text: str,
        published: tuple[MessageReference, ...],
    ) -> None:
        self._flush_thinking()
        item_id = self._reply_item_id or NativeItemId.fresh()
        started_at = self._reply_started_at or time.time()
        self._reply_item_id = None
        self._reply_parts.clear()
        self._last_draft_at = 0.0
        if not text.strip():
            return
        self._ensure_opened()
        event = AssistantTranscript(text, position=item_id, timestamp=started_at)
        if published:
            event = event.with_publications(published)
        self.transcript.finish(
            turn_id=self.turn_id,
            item_id=item_id,
            events=(event,),
        )

    def on_finish(self, published: tuple[MessageReference, ...]) -> None:
        self._flush_thinking()
        if self._reply_parts:
            body = "".join(self._reply_parts)
            item_id = self._reply_item_id or NativeItemId.fresh()
            started_at = self._reply_started_at or time.time()
            self._reply_item_id = None
            self._reply_parts.clear()
            self._last_draft_at = 0.0
            if body.strip():
                self._ensure_opened()
                event = AssistantTranscript(body, position=item_id, timestamp=started_at)
                if published:
                    event = event.with_publications(published)
                self.transcript.finish(
                    turn_id=self.turn_id,
                    item_id=item_id,
                    events=(event,),
                )
        if self._opened:
            self.transcript.finish_replies(self.turn_id)
            self.transcript.end_turn()

    def on_abort(self) -> None:
        self._flush_thinking()
        self._reply_item_id = None
        self._reply_parts.clear()
        self._last_draft_at = 0.0
        if self._opened:
            self.transcript.finish_replies(self.turn_id)
            self.transcript.end_turn()
