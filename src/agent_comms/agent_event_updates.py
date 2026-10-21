"""ACP publication of typed backend observations; SDK formats remain unchanged."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    ContentToolCallContent,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
)

from . import agent_events as events
from .acp_extension import (
    CompactionChangedUpdate,
    McpClientAcknowledgementUpdate,
    PromptCancelledUpdate,
    encode_updates,
)
from .context_reading import ContextUsage, UnmeasuredContext
from .coordinator import Coordination
from .mro_dispatch import MroDispatch, handles
from .tool_results import tool_result_content

if TYPE_CHECKING:
    from acp.agent.connection import AgentSideConnection

    from .acp import CommsAgent
    from .routing import MessageRoute
    from .runtime import RuntimeServer, SocketClient


class AcpEventConsumer(MroDispatch):
    def __init__(
        self,
        agent: CommsAgent,
        session_id: str,
        client: AgentSideConnection | RuntimeServer | SocketClient,
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
        await self.agent.sessions.publish_metadata(self.session_id)

    @handles(events.Chunk, events.Notice)
    async def text(self, event: events.Chunk | events.Notice) -> None:
        if event.text:
            await self.agent._emit_text(self.session_id, event.text, self.client, self.route)

    @handles(events.ToolStart)
    async def on_tool_start(self, event: events.ToolStart) -> None:
        session_id = self.session_id
        client = self.client
        start_update = ToolCallStart(
            session_update="tool_call",
            tool_call_id=event.id,
            title=event.title or event.name or "tool",
            kind=cast(Any, event.kind),
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
                for item in tool_result_content(event.id, event.output or "", event.diff, event.sent_message)
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
        if not turn_id or not await Coordination.run_worker(partial(self.agent.turns.owns_turn, session_id, turn_id)):
            return
        await client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(McpClientAcknowledgementUpdate(turn_id, event.acknowledgement)),
            ),
        )

    @handles(events.ContextInfo)
    async def on_context_info(self, event: events.ContextInfo) -> None:
        await self.dispatch(event.context_size.reading(event.context_used))

    @handles(ContextUsage)
    async def on_context_usage(self, usage: ContextUsage) -> None:
        await self.client.session_update(
            session_id=self.session_id,
            update=UsageUpdate(session_update="usage_update", used=usage.used, size=usage.size),
        )

    @handles(UnmeasuredContext)
    async def on_unmeasured_context(self, _reading: UnmeasuredContext) -> None:
        await self.agent.sessions.publish_metadata(self.session_id)

    @handles(events.CompactionEvent)
    async def on_compaction(
        self, event: events.CompactionEvent
    ) -> None:
        await self.agent.turns.observe_compaction(self.session_id, event)
        await self.client.session_update(
            session_id=self.session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(CompactionChangedUpdate(event)),
            ),
        )

    @handles(events.Done)
    async def on_done(self, event: events.Done) -> events.Done:
        # Notification and request failure consume the same resolved outcome.
        return replace(event, outcome=await event.outcome.publish(self.client, self.session_id))

    @handles(events.PromptCancelled)
    async def on_cancelled(self, event):
        await self.client.session_update(
            session_id=self.session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(PromptCancelledUpdate(event.input_state)),
            ),
        )
