"""Antigravity JSONL transcript records decoded into Core's RuntimeUpdate stream."""
from __future__ import annotations

import re
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import ClassVar

from pydantic import Field, JsonValue

from agent_comms.cli_bridges.claude_protocol import ClaudeMonitorFrame
from agent_comms.cli_bridges.cli_protocol import HistoryRecord, TaggedWire
from agent_comms.cli_bridges.runtime_updates import (
    ActiveItem,
    FinishedItem,
    FinishedTurn,
    ReadingPhase,
    RuntimeInfoObserved,
    RuntimeUpdate,
    StartedItem,
    StatusUpdate,
    Summarized,
    ToolDraft,
    TurnStarted,
)
from agent_comms.cli_bridges.shared import (
    MonitorObservation,
    ObservedMonitor,
)
from agent_comms.cli_bridges.tool_calls import ToolCall
from agent_comms.cli_bridges.wire_models import WireModel
from agent_comms.execution.runtime_observations import TranscriptModelUsage
from agent_comms.execution.turn_item import NamedSourceTurn, NativeItemId, NativeTurnId
from agent_comms.execution.turn_phase import ModelWaitPhase, ToolRunningPhase
from agent_comms.nominal.field_codec import TimestampText, ToolCallId, TranscriptRecordId
from agent_comms.pi_counts import ReportedCount, UnreportedCount
from agent_comms.registry.thread_model import ThreadModel
from agent_comms.storage.recorded_time import EventTime
from agent_comms.transcript.transcript_events import (
    AssistantTranscript,
    NoticeTranscript,
    ThinkingTranscript,
    ToolStartTranscript,
    UserTranscript,
)

_USER_REQUEST_RE = re.compile(
    r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>",
    re.DOTALL,
)


def _strip_user_request(text: str) -> str:
    match = _USER_REQUEST_RE.search(text)
    return match.group(1).strip() if match else text.strip()


class AntigravityToolCallWire(WireModel):
    """One tool invocation inside an Antigravity `PLANNER_RESPONSE` step."""

    name: str
    args: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass
class AntigravityToolDraft(ToolDraft):
    """One open Antigravity tool call waiting for its matching `GENERIC` completion step."""

    call: ToolCall
    item_id: NativeItemId


class AntigravityReaderPhase(ReadingPhase):
    """Initial catch-up window suppresses stale activity until `window_ended` fires."""

    @abstractmethod
    def window_ended(self, session: AntigravitySession) -> tuple[RuntimeUpdate, ...]: ...


@dataclass
class AntigravityCatchUp(AntigravityReaderPhase):
    pending: tuple[StatusUpdate, ...] = ()
    latest_info: tuple[RuntimeInfoObserved, ...] = ()

    def runtime_info(self, update: RuntimeInfoObserved) -> tuple[RuntimeUpdate, ...]:
        self.latest_info = (update,)
        return ()

    def status(self, update: StatusUpdate) -> tuple[RuntimeUpdate, ...]:
        self.pending = (update,)
        return ()

    def window_ended(self, session: AntigravitySession) -> tuple[RuntimeUpdate, ...]:
        session.phase = AntigravityLive()
        return (*self.latest_info, *self.pending)


class AntigravityLive(AntigravityReaderPhase):
    def status(self, update: StatusUpdate) -> tuple[RuntimeUpdate, ...]:
        return (update,)

    def window_ended(self, session: AntigravitySession) -> tuple[RuntimeUpdate, ...]:
        return ()


@dataclass
class AntigravitySession:
    """Mutable decoding cursor for one Antigravity `transcript.jsonl` stream."""

    turn_id: NativeTurnId = field(default_factory=lambda: NativeTurnId("agy-turn-0"))
    pending_tools: deque[AntigravityToolDraft] = field(default_factory=deque)
    phase: AntigravityReaderPhase = field(default_factory=AntigravityCatchUp)
    answered_by: TranscriptModelUsage = field(default_factory=TranscriptModelUsage)

    def open_turn(self, step_index: int) -> NativeTurnId:
        self.turn_id = NativeTurnId(f"agy-turn-{step_index}")
        self.pending_tools.clear()
        return self.turn_id

    def usage_reported(
        self, model: str, input_tokens: int, cache_read_tokens: int
    ) -> tuple[RuntimeUpdate, ...]:
        used = input_tokens + cache_read_tokens
        if not model and used <= 0:
            return ()
        observation = TranscriptModelUsage(
            model=ThreadModel.reported(model) if model else ThreadModel(),
            context_used=ReportedCount(used) if used > 0 else UnreportedCount(),
        )
        if observation == self.answered_by:
            return ()
        self.answered_by = observation
        return (RuntimeInfoObserved(observation),)


class AntigravityRecord(TaggedWire, HistoryRecord, affix=""):
    """One line of Antigravity's `transcript.jsonl`."""

    step_index: int = 0
    source: str = ""
    status: str = ""
    created_at: str = ""
    position: int = 0

    @classmethod
    def undeclared_tag(cls, tag: str) -> str:
        del tag
        return UnknownAntigravityRecord.tag()

    @property
    def timestamp(self) -> float:
        if not self.created_at:
            return 0.0
        return TimestampText.from_text(self.created_at).at.timestamp()

    def updates(self, session: AntigravitySession) -> tuple[RuntimeUpdate, ...]:
        del session
        return ()

    def compacted_at(self) -> tuple[tuple[int, str], ...]:
        """Step index and timestamp when this record marks a compaction boundary."""
        return ()


class UserInputRecord(AntigravityRecord, declared_name="USER_INPUT"):
    """Explicit user turn starting a new conversation exchange."""

    content: str = ""

    @property
    def clean_text(self) -> str:
        return _strip_user_request(self.content)

    def request(self) -> str:
        return self.clean_text

    def owner_words(self) -> tuple[str, ...]:
        text = self.clean_text
        return (text,) if text else ()

    def updates(self, session: AntigravitySession) -> tuple[RuntimeUpdate, ...]:
        text = self.clean_text
        if not text:
            return ()
        turn_id = session.open_turn(self.step_index)
        item_id = NativeItemId(f"agy-user-{self.step_index}")
        event = UserTranscript(text, position=self.position, timestamp=self.timestamp)
        return (
            TurnStarted(turn_id),
            FinishedItem(turn_id=turn_id, item_id=item_id, events=(event,)),
            ActiveItem(turn_id, item_id, ModelWaitPhase()),
        )


class PlannerResponseRecord(AntigravityRecord, declared_name="PLANNER_RESPONSE"):
    """Model step containing thinking, tool calls, and/or final assistant reply."""

    model: str = ""
    content: str = ""
    thinking: str = ""
    input_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    tool_calls: list[AntigravityToolCallWire] = Field(default_factory=list)

    def updates(self, session: AntigravitySession) -> tuple[RuntimeUpdate, ...]:
        emitted: list[RuntimeUpdate] = list(
            session.usage_reported(self.model, self.input_tokens, self.cache_read_tokens)
        )
        turn_id = session.turn_id
        ts = self.timestamp
        if self.thinking.strip():
            think_id = NativeItemId(f"agy-think-{self.step_index}")
            emitted.append(
                FinishedItem(
                    turn_id=turn_id,
                    item_id=think_id,
                    events=(ThinkingTranscript(self.thinking, position=self.position, timestamp=ts),),
                )
            )
        for index, raw_call in enumerate(self.tool_calls):
            call_id = ToolCallId(f"agy-{self.step_index}-{index}")
            item_id = NativeItemId(f"agy-tool-{self.step_index}-{index}")
            call = ToolCall(id=call_id, name=raw_call.name, arguments=dict(raw_call.args))
            start_event: ToolStartTranscript = call.started(self.position, ts)
            draft = AntigravityToolDraft(
                started_at=EventTime.capture(ts),
                prefix=(start_event,),
                call=call,
                item_id=item_id,
            )
            session.pending_tools.append(draft)
            emitted.append(
                StartedItem(
                    turn_id,
                    item_id,
                    ToolRunningPhase(tools=((call.id, call.title),)),
                    draft,
                )
            )
        if self.content.strip():
            reply_id = NativeItemId(f"agy-reply-{self.step_index}")
            emitted.append(
                FinishedItem(
                    turn_id=turn_id,
                    item_id=reply_id,
                    events=(AssistantTranscript(self.content, position=self.position, timestamp=ts),),
                )
            )
            if not self.tool_calls:
                emitted.append(FinishedTurn(turn_id))
        return tuple(emitted)


class GenericStepRecord(AntigravityRecord, declared_name="GENERIC"):
    """Tool execution update (`RUNNING`, `DONE`, or `ERROR`)."""

    content: str = ""
    error: str = ""

    def updates(self, session: AntigravitySession) -> tuple[RuntimeUpdate, ...]:
        turn_id = session.turn_id
        ts = self.timestamp
        if self.status == "RUNNING":
            if session.pending_tools:
                head = session.pending_tools[0]
                return (
                    ActiveItem(
                        turn_id,
                        head.item_id,
                        ToolRunningPhase(tools=((head.call.id, head.call.title),)),
                    ),
                )
            return ()
        ok = self.status != "ERROR"
        body = self.error if (not ok and self.error) else self.content
        observations = _monitor_observations(self, body, session, open_turn=False) if ok else ()
        if session.pending_tools:
            draft = session.pending_tools.popleft()
            events = (*draft.events, draft.call.ended(body, ok, self.position, ts))
            return (
                FinishedItem(turn_id=turn_id, item_id=draft.item_id, events=events),
                ActiveItem(turn_id, draft.item_id, ModelWaitPhase()),
                *observations,
            )
        if not ok and body:
            err_id = NativeItemId(f"agy-err-{self.step_index}")
            return (
                FinishedItem(
                    turn_id=turn_id,
                    item_id=err_id,
                    events=(NoticeTranscript(body, position=self.position, timestamp=ts),),
                ),
            )
        return observations


class CheckpointRecord(AntigravityRecord, declared_name="CHECKPOINT"):
    """Compaction checkpoint marking where current history begins."""

    content: str = ""

    def begins_history(self) -> bool:
        return True

    def compacted_at(self) -> tuple[tuple[int, str], ...]:
        return ((self.step_index, self.created_at),)

    def updates(self, session: AntigravitySession) -> tuple[RuntimeUpdate, ...]:
        del session
        return (Summarized(self.content),) if self.content else ()


_COMMAND_OUTPUT_MARKER = "Command output:\n"
_LOG_TRAILER_RE = re.compile(r"\nLog: file://.*$", re.DOTALL)
_CRLF_RE = re.compile(r"\r+\n")


def _extract_command_output(content: str) -> str:
    """Return the normalized stdout body from an Antigravity command completion record."""
    normalized = _CRLF_RE.sub("\n", content).replace("\r", "\n")
    marker = normalized.find(_COMMAND_OUTPUT_MARKER)
    if marker < 0:
        return ""
    body = normalized[marker + len(_COMMAND_OUTPUT_MARKER) :]
    body = _LOG_TRAILER_RE.sub("", body)
    body = body.removesuffix("</SYSTEM_MESSAGE>")
    return body.rstrip("\n")


def _monitor_observations(
    record: AntigravityRecord,
    content: str,
    session: AntigravitySession,
    *,
    open_turn: bool,
) -> tuple[RuntimeUpdate, ...]:
    output = _extract_command_output(content)
    if not output:
        return ()
    frames = tuple(
        observed
        for frame in ClaudeMonitorFrame.decode_event(output)
        for observed in frame.observed_frames()
    )
    if not frames:
        return ()
    turn_id = session.open_turn(record.step_index) if open_turn else session.turn_id
    record_id = TranscriptRecordId(
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"antigravity:{record.step_index}:{record.created_at}"))
    )
    observed = ObservedMonitor(record_id, record.timestamp)
    source_turn = NamedSourceTurn(turn_id)
    observations = tuple(
        MonitorObservation(frame, observed, source_turn=source_turn) for frame in frames
    )
    return (TurnStarted(turn_id), *observations) if open_turn else observations


class SystemMessageRecord(AntigravityRecord, declared_name="SYSTEM_MESSAGE"):
    """Internal system message injected by Antigravity, including background-task wake outputs."""

    content: str = ""

    def updates(self, session: AntigravitySession) -> tuple[RuntimeUpdate, ...]:
        return _monitor_observations(self, self.content, session, open_turn=True)


class UnknownAntigravityRecord(AntigravityRecord, declared_name="unknown"):
    """Forward-compatible fallback for newly added Antigravity step types."""


ANTIGRAVITY_RECORD = WireModel.adapter(AntigravityRecord.declared_union())
