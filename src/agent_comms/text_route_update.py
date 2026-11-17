from __future__ import annotations

from dataclasses import dataclass

from acp.schema import AgentMessageChunk, TextContentBlock

from agent_comms.execution.reported_turn import ReportedTurnState
from agent_comms.messaging.routing import MessageRoute, NoMessageRoute
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import AcpSessionId, FieldCodec


class AgentCommsUpdate(DeclaredFamily, affix="Update"):
    """One declared fact; member identity supplies its wire discriminator."""

    def for_session(self, session_id: AcpSessionId) -> AgentCommsUpdate:
        return self

    def matches_turn(self, turn: ReportedTurnState) -> bool:
        return True

    def acp_chunk(self) -> AgentMessageChunk:
        """Carry this declared observation in its original empty ACP envelope."""
        return AgentMessageChunk(
            session_update="agent_message_chunk",
            content=TextContentBlock(type="text", text=""),
            field_meta=encode_updates(self),
        )


@dataclass(frozen=True)
class TextRouteUpdate(AgentCommsUpdate):
    route: MessageRoute | NoMessageRoute = NoMessageRoute()


@dataclass(frozen=True)
class UpdateBatch:
    updates: tuple[AgentCommsUpdate, ...]


def encode_updates(*updates: AgentCommsUpdate) -> dict:
    return {"agentComms": FieldCodec.encode(UpdateBatch(updates))}
