"""ACP publication of typed backend observations; SDK formats remain unchanged."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    ContentToolCallContent,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolKind,
    UsageUpdate,
    SessionInfoUpdate,
)
from metaclass_registry.mro_dispatch import (
    MroDispatch,
    handles,
)

from . import agent_events as events
from .acp_extension import (
    CompactionChangedUpdate,
    GoalChangedUpdate,
    McpClientAcknowledgementUpdate,
    NativeOutputCommittedUpdate,
    NativeRoundCommittedUpdate,
    PromptCancelledUpdate,
    TextNoticeUpdate,
    TextRouteUpdate,
    encode_updates,
)
from .context_reading import ContextUsage, UnsizedContext
from .controller import SessionUpdateSink
from .coordinator import Coordination
from .field_codec import AcpSessionId, TurnId
from .tool_declaration import (
    EditAction,
    ExecuteAction,
    OtherAction,
    ReadAction,
    SearchAction,
    ToolAction,
)
from .tool_results import tool_result_content
from .transcript_updates import TranscriptUpdate
from .routing import MessageRoute, NoMessageRoute
from .turn_effects import TurnEffects

if TYPE_CHECKING:
    from .acp_extension import AgentCommsUpdate
    from .native_output import NativeRecordLocation
    from .transcripts import NativeCommittedPrefix


class AcpToolAction(MroDispatch):
    """Translate the tool's capability only when building an ACP message."""

    @handles(SearchAction)
    @staticmethod
    def search(action: type[SearchAction]) -> ToolKind:
        return "search"

    @handles(ReadAction)
    @staticmethod
    def read(action: type[ReadAction]) -> ToolKind:
        return "read"

    @handles(ExecuteAction)
    @staticmethod
    def execute(action: type[ExecuteAction]) -> ToolKind:
        return "execute"

    @handles(EditAction)
    @staticmethod
    def edit(action: type[EditAction]) -> ToolKind:
        return "edit"

    @handles(OtherAction)
    @staticmethod
    def other(action: type[OtherAction]) -> ToolKind:
        return "other"

    @classmethod
    def kind(cls, action: type[ToolAction]) -> ToolKind:
        kind: ToolKind = cls.handler_for(action)(action)
        return kind


@dataclass(eq=False, repr=False)
class AcpEventConsumer(MroDispatch):
    agent: TurnEffects
    session_id: AcpSessionId
    client: SessionUpdateSink

    @handles(TranscriptUpdate)
    async def transcript(self, event: TranscriptUpdate) -> None:
        await event.publish(self.session_id, self.client)

    def text_metadata(self, event: events.TextEvent) -> tuple[AgentCommsUpdate, ...]:
        return event.acp_updates(TextNoticeUpdate)

    def text_update(self, event: events.TextEvent) -> AgentMessageChunk:
        return AgentMessageChunk(
            session_update="agent_message_chunk",
            content=TextContentBlock(type="text", text=event.text),
            field_meta=encode_updates(*self.text_metadata(event)),
        )

    @handles(events.NativeOutputCommitted)
    async def native_output_committed(self, event: events.NativeOutputCommitted) -> None:
        await self.publish_native_commit(
            event.location, partial(NativeOutputCommittedUpdate, output=event.location.output)
        )

    @handles(events.NativeRoundCommitted)
    async def native_round_committed(self, event: events.NativeRoundCommitted) -> None:
        await self.publish_native_commit(event.location, NativeRoundCommittedUpdate)

    async def publish_native_commit(
        self,
        location: NativeRecordLocation,
        update: Callable[[NativeCommittedPrefix], AgentCommsUpdate],
    ) -> None:
        sessions = self.agent.sessions
        identity = await Coordination.run_worker(
            partial(
                sessions.comms.transcripts.capture_native_output,
                sessions.require(self.session_id),
                location,
            )
        )
        await self.client.publish_update(
            session_id=self.session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(update(identity)),
            ),
        )

    @handles(events.GoalChanged)
    async def goal_changed(self, event: events.GoalChanged) -> None:
        await self.client.publish_update(
            session_id=self.session_id,
            update=SessionInfoUpdate(
                session_update="session_info_update",
                field_meta=encode_updates(GoalChangedUpdate.from_snapshot(event.snapshot)),
            ),
        )

    @handles(events.TextEvent)
    async def text(self, event: events.TextEvent) -> None:
        if event.text:
            await self.client.publish_update(
                session_id=self.session_id, update=self.text_update(event)
            )

    @handles(events.ToolStart)
    async def on_tool_start(self, event: events.ToolStart) -> None:
        session_id = self.session_id
        client = self.client
        start_update = ToolCallStart(
            session_update="tool_call",
            tool_call_id=event.id,
            title=event.title or event.name or "tool",
            kind=AcpToolAction.kind(event.kind),
            status="in_progress",
        )
        if event.args is not None:
            start_update.raw_input = event.args
        await client.publish_update(
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
        await client.publish_update(session_id=session_id, update=progress_update)

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
                for item in tool_result_content(
                    event.id, event.output or "", event.diff, event.sent_message
                )
            ],
        )
        await client.publish_update(session_id=session_id, update=end_update)

    @handles(events.Thinking)
    async def on_thinking(self, event: events.Thinking) -> None:
        session_id = self.session_id
        client = self.client
        await client.publish_update(
            session_id=session_id,
            update=AgentThoughtChunk(
                session_update="agent_thought_chunk",
                content=TextContentBlock(type="text", text=event.text or ""),
            ),
        )

    @handles(events.ContextInfo)
    async def on_context_info(self, event: events.ContextInfo) -> None:
        await self.dispatch(event.context_size.reading(event.context_used))

    @handles(ContextUsage)
    async def on_context_usage(self, usage: ContextUsage) -> None:
        await self.agent.turns.observe_context(self.session_id, usage)
        await self.client.publish_update(
            session_id=self.session_id,
            update=UsageUpdate(session_update="usage_update", used=usage.used, size=usage.size),
        )

    @handles(UnsizedContext)
    async def on_unsized_context(self, _reading: UnsizedContext) -> None:
        await self.agent.sessions.publish_metadata(self.session_id)

    @handles(events.CompactionEvent)
    async def on_compaction(self, event: events.CompactionEvent) -> None:
        await self.agent.turns.observe_compaction(self.session_id, event)
        await self.client.publish_update(
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
        await self.client.publish_update(
            session_id=self.session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(PromptCancelledUpdate(event.input_state)),
            ),
        )


@dataclass(eq=False, repr=False)
class TurnEventConsumer(AcpEventConsumer):
    turn_id: TurnId

    @handles(events.McpLiveStatus)
    async def on_mcp_live_status(self, event: events.McpLiveStatus) -> None:
        if not await Coordination.run_worker(
            partial(self.agent.turns.owns_turn, self.session_id, self.turn_id)
        ):
            return
        await self.client.publish_update(
            session_id=self.session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(
                    McpClientAcknowledgementUpdate(self.turn_id, event.acknowledgement)
                ),
            ),
        )


@dataclass(eq=False, repr=False)
class Routed(AcpEventConsumer):
    route: MessageRoute | NoMessageRoute

    def text_metadata(self, event: events.TextEvent) -> tuple[AgentCommsUpdate, ...]:
        return (*super().text_metadata(event), TextRouteUpdate(self.route))


@dataclass(eq=False, repr=False)
class RoutedTurnEventConsumer(Routed, TurnEventConsumer):
    """A turn-scoped publication whose text also answers a bus route."""
