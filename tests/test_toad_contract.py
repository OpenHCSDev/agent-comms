"""Public agent-comms and ACP fields consumed by pinned Toad builds."""

from dataclasses import asdict

from acp.schema import AgentMessageChunk, NewSessionResponse, TextContentBlock

from agent_comms import Comms, Goal, MessageRoute, Thread, TranscriptCursor, TranscriptPage, wire
from agent_comms.acp import CommsAgent


def test_toad_public_types_and_acp_agent_comms_metadata(tmp_path) -> None:
    assert all(
        isinstance(public_type, type)
        for public_type in (Comms, Goal, TranscriptCursor, TranscriptPage, MessageRoute)
    )
    comms = wire(tmp_path / "wire")
    assert isinstance(comms, Comms)
    comms.register(Thread("worker", frozenset(), str(tmp_path)))
    agent = CommsAgent(comms, agent_bin="nonexistent-pi", runtime_enabled=False, auto_wake=False)

    session = NewSessionResponse(
        session_id="worker", field_meta=agent._session_metadata("worker")
    ).model_dump(by_alias=True, exclude_none=True)
    assert session["_meta"]["agentComms"]["thread"] == "worker"
    assert session["_meta"]["agentComms"]["wireRoot"] == str(comms.root.resolve())

    route = MessageRoute("worker", ("#team",))
    update = AgentMessageChunk(
        session_update="agent_message_chunk",
        content=TextContentBlock(type="text", text="routed reply"),
        field_meta={"agentComms": {"route": asdict(route)}},
    ).model_dump(by_alias=True, exclude_none=True)
    assert update["_meta"]["agentComms"]["route"] == asdict(route)
    assert MessageRoute.from_wire(update["_meta"]["agentComms"]["route"]) == route
