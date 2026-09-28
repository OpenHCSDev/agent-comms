"""ACP publication of typed backend observations; SDK formats remain unchanged."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any, cast

from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    ContentToolCallContent,
    SessionInfoUpdate,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
)

from . import agent_events as events
from . import backend
from .mro_dispatch import MroDispatch, handles
from .tool_results import tool_result_content

if TYPE_CHECKING:
    from .acp import CommsAgent
    from .routing import MessageRoute


class AcpEventConsumer(MroDispatch):
    def __init__(
        self,
        agent: CommsAgent,
        session_id: str,
        client: Any,
        turn_id: str | None = None,
        route: MessageRoute | None = None,
    ) -> None:
        self.agent = agent
        self.session_id = session_id
        self.client = client
        self.turn_id = turn_id
        self.route = route

    @handles(events.GoalChanged)
    async def goal_changed(self, event: events.GoalChanged) -> None:
        await self.client.session_update(
            session_id=self.session_id,
            update=SessionInfoUpdate(
                session_update="session_info_update",
                field_meta={
                    "agentComms": {
                        "goal": event.goal.to_wire() if event.goal else None,
                        "goalExecution": asdict(event.execution) if event.execution else None,
                    }
                },
            ),
        )

    @handles(events.Chunk, events.Notice)
    async def text(self, event: events.Chunk | events.Notice) -> None:
        if event.text:
            await self.agent._emit_text(self.session_id, event.text, self.client, self.route)

    async def settled(self, turn_id: str) -> None:
        await self.client.session_update(
            session_id=self.session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta={"agentComms": {"turnSettled": True, "turnId": turn_id}},
            ),
        )

    @handles(events.StreamSettled)
    async def stream_settled(self, event: events.StreamSettled) -> None:
        assert self.turn_id is not None
        await self.settled(self.turn_id)

    @handles(events.TurnSettled)
    async def turn_settled(self, event: events.TurnSettled) -> None:
        await self.settled(event.turn_id)

    @handles(events.NoActiveTurn)
    async def no_active_turn(self, event: events.NoActiveTurn) -> None:
        # Preserve the existing ACP replay format only at the external boundary.
        await self.settled("")

    @handles(events.ToolStart)
    async def on_tool_start(self, event: events.ToolStart) -> None:
        session_id = self.session_id
        client = self.client
        start_update = ToolCallStart(
            session_update="tool_call",
            tool_call_id=event.id,
            title=event.title or event.name or "tool",
            kind=cast(Any, backend.tool_kind(event.name or "other")),
            status="in_progress",
        )
        if event.args is not None:
            start_update.raw_input = event.args
        await client.session_update(
            session_id=session_id,
            update=start_update,
        )

    @handles(events.ToolProgress)
    async def on_tool_progress(self, event: events.ToolProgress) -> None:
        session_id = self.session_id
        client = self.client
        progress_update = ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id=event.id,
            status="in_progress",
        )
        output = event.output or ""
        if output:
            progress_update.content = [
                ContentToolCallContent(
                    type="content",
                    content=TextContentBlock(type="text", text=output),
                )
            ]
        await client.session_update(session_id=session_id, update=progress_update)

    @handles(events.ToolEnd)
    async def on_tool_end(self, event: events.ToolEnd) -> None:
        session_id = self.session_id
        client = self.client
        end_update = ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id=event.id,
            status="completed" if event.ok else "failed",
            content=[
                ContentToolCallContent.model_validate(item)
                for item in tool_result_content(event.id, event.output or "", event.diff)
            ],
        )
        await client.session_update(session_id=session_id, update=end_update)

    @handles(events.Thinking)
    async def on_thinking(self, event: events.Thinking) -> None:
        session_id = self.session_id
        client = self.client
        await client.session_update(
            session_id=session_id,
            update=AgentThoughtChunk(
                session_update="agent_thought_chunk",
                content=TextContentBlock(type="text", text=event.text or ""),
            ),
        )

    @handles(events.McpLiveStatus)
    async def on_mcp_live_status(self, event: events.McpLiveStatus) -> None:
        session_id = self.session_id
        client = self.client
        turn_id = self.turn_id
        if not turn_id or self.agent.turns.active_turns.get(session_id) != turn_id:
            return
        await client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta={"agentComms": {"turnId": turn_id, "mcpClient": event.receipt}},
            ),
        )

    @handles(events.AgentInfo)
    async def on_agent_info(self, event: events.AgentInfo) -> None:
        session_id = self.session_id
        client = self.client
        used = event.context_used
        size = event.context_size
        if used is not None and size:
            await client.session_update(
                session_id=session_id,
                update=UsageUpdate(
                    session_update="usage_update",
                    used=used,
                    size=size,
                ),
            )

    @handles(events.CompactionProgress)
    async def on_compaction_progress(self, event: events.CompactionProgress) -> None:
        session_id = self.session_id
        client = self.client
        chunk_index = event.chunk_index
        done = event.source_bytes_done
        total = event.source_bytes_total
        measured = done is not None and total is not None and 0 <= done <= total and total > 0
        if chunk_index > 0 or chunk_index == 0 and measured:
            await client.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=""),
                    field_meta={
                        "agentComms": {
                            "compaction": {
                                "phase": "progress",
                                "status": "running",
                                "chunkIndex": chunk_index,
                                **(
                                    {"sourceBytesDone": done, "sourceBytesTotal": total}
                                    if measured
                                    else {}
                                ),
                                **(
                                    {"summaryPhase": event.summary_phase}
                                    if event.summary_phase
                                    else {}
                                ),
                            }
                        }
                    },
                ),
            )

    @handles(events.CompactionEvent)
    async def on_compaction(self, event: events.CompactionEvent) -> None:
        session_id = self.session_id
        client = self.client
        phase = event.phase
        reason = event.reason
        if reason not in {"manual", "threshold", "overflow", "unknown"}:
            reason = "unknown"
        summary = backend.compaction_summary(event.publication_summary)
        status = {"start": "running", "end": "completed", "abort": "aborted"}[phase]
        status_text = {
            "start": "",
            "end": "Context compacted; usage is recalculating.",
            "abort": "Context compaction aborted; usage is unknown.",
        }[phase]
        if summary:
            status_text += f" {event.summary_label}{summary}"
        detail: dict[str, Any] = {
            "phase": phase,
            "status": status,
            "reason": reason,
            "contextUsed": None,
            "contextState": "unknown",
            "willRetry": event.will_retry is True,
        }
        if summary:
            detail["summary"] = summary
        await client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=status_text),
                field_meta={"agentComms": {"compaction": detail}},
            ),
        )

    @handles(events.Error)
    async def on_error(self, event: events.Error) -> None:
        session_id = self.session_id
        client = self.client
        text = str(event.text or "Backend failed")
        self.agent.turns.emitted_errors[session_id] = text
        failed_input = None
        input_text = self.agent.inputs.turn_input_text.get(session_id)
        if input_text and not self.agent.inputs.dispositions.read().all_started(
            self.agent.inputs.turn_original_input_keys.get(session_id, ())
        ):
            failed_input = {"text": input_text, "reason": text}
        await client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=f"[agent error] {text}"),
                field_meta={
                    "agentComms": {
                        **({"inputFailed": failed_input} if failed_input else {}),
                        "route": None,
                    }
                },
            ),
        )

    @handles(events.Done)
    async def on_done(self, event: events.Done) -> None:
        session_id = self.session_id
        client = self.client
        prior_error = self.agent.turns.emitted_errors.pop(session_id, None)
        if not event.ok and event.text:
            text = str(event.text)
            # An explicit error event in this turn already showed the failure.
            if prior_error != text:
                await self.agent._emit_event(session_id, events.Error(text), client)
                self.agent.turns.emitted_errors.pop(session_id, None)
