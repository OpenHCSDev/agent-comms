"""Public agent-comms and ACP fields consumed by pinned Toad builds."""

from dataclasses import asdict

import pytest
from acp.schema import NewSessionResponse

from agent_comms import Comms, Goal, MessageRoute, Thread, TranscriptCursor, TranscriptPage, wire
from agent_comms.acp import CommsAgent


@pytest.mark.asyncio
async def test_toad_public_types_and_acp_agent_comms_metadata(tmp_path) -> None:
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

    updates = []

    class Client:
        async def session_update(self, session_id, update):
            assert session_id == "worker"
            updates.append(update.model_dump(by_alias=True, exclude_none=True))

    route = MessageRoute("worker", ("#team",))
    await agent._emit_text("worker", "routed reply", Client(), route)
    assert len(updates) == 1
    update = updates[0]
    assert update["content"]["text"] == "routed reply"
    assert update["_meta"]["agentComms"]["route"] == asdict(route)
    assert MessageRoute.from_wire(update["_meta"]["agentComms"]["route"]) == route
