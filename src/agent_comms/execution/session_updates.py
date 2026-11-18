"""Declared session facts and their shared encoding, independent of routing and ACP."""

from __future__ import annotations

from dataclasses import dataclass

from agent_comms.execution.reported_turn import ReportedTurnState
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import AcpSessionId, FieldCodec


class AgentCommsUpdate(DeclaredFamily, affix="Update"):
    """One declared fact; member identity supplies its wire discriminator."""

    def for_session(self, session_id: AcpSessionId) -> AgentCommsUpdate:
        return self

    def matches_turn(self, turn: ReportedTurnState) -> bool:
        return True


@dataclass(frozen=True)
class UpdateBatch:
    updates: tuple[AgentCommsUpdate, ...]


def encode_updates(*updates: AgentCommsUpdate) -> dict:
    return {"agentComms": FieldCodec.encode(UpdateBatch(updates))}
