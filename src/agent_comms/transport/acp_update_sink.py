"""ACP envelope construction belongs to the transport, not the shared fact declarations."""

from __future__ import annotations

from acp.schema import AgentMessageChunk, TextContentBlock
from pydantic import JsonValue

from agent_comms.execution.controller import SessionUpdateSink
from agent_comms.nominal.field_codec import AcpSessionId


class AcpUpdateSink(SessionUpdateSink):
    """ACP transports wrap the shared fact encoding in their SDK envelope."""

    async def publish_metadata(
        self, *, session_id: AcpSessionId, metadata: dict[str, JsonValue],
    ) -> bool:
        return await self.publish_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=metadata,
            ),
        )
