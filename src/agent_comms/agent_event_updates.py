"""ACP publication of typed backend observations; SDK formats remain unchanged."""

from __future__ import annotations

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
from .acp_extension import (
    CompactionChangedUpdate,
    GoalChangedUpdate,
    InputFailedUpdate,
    McpClientReceiptUpdate,
    PromptCancelledUpdate,
    RequestFailedUpdate,
    TextRouteUpdate,
    encode_updates,
)
from .acp_failure import ACPFailure
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
                field_meta=encode_updates(GoalChangedUpdate(event.goal, event.execution)),
            ),
        )

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
        if not turn_id or not self.agent.turns.owns_turn(session_id, turn_id):
            return
        await client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(McpClientReceiptUpdate(turn_id, event.receipt)),
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

    @handles(events.Error)
    async def on_error(self, event: events.Error) -> None:
        session_id = self.session_id
        client = self.client
        text = str(event.text or "Backend failed")
        failure = ACPFailure.from_error(-32603, text, diagnostics=event.diagnostics)
        original = self.agent.inputs.original_sources.get(session_id)
        original_keys = original.notice_keys if original else ()
        from dataclasses import replace

        failure = replace(
            failure,
            input_state=self.agent.inputs.dispositions.read().shared_state(original_keys),
        )
        if self.agent.turns.emitted_errors.get(session_id) == failure:
            return
        failed_input = None
        input_text = original.notice_text if original else None
        if input_text and not self.agent.inputs.dispositions.read().all_started(original_keys):
            failed_input = InputFailedUpdate(input_text, failure)
        await client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(
                    TextRouteUpdate(None),
                    RequestFailedUpdate(failure),
                    *((failed_input,) if failed_input else ()),
                ),
            ),
        )
        self.agent.turns.emitted_errors[session_id] = failure

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

    @handles(events.Done)
    async def on_done(self, event: events.Done) -> None:
        session_id = self.session_id
        client = self.client
        if not event.ok and event.text:
            # The existing emission owner deduplicates full typed evidence,
            # including a terminal not-sent transition with unchanged text.
            await self.agent._emit_event(session_id, events.Error(str(event.text)), client)
