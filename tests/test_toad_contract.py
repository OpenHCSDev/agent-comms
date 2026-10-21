"""Public agent-comms and ACP fields consumed by pinned Toad builds."""

import pytest
from acp.schema import NewSessionResponse

from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import CoordinationChangedUpdate, TextRouteUpdate, decode_updates
from agent_comms.comms import Comms, wire
from agent_comms.goals import Goal
from agent_comms.routing import MessageRoute
from agent_comms.threads import Thread
from agent_comms.transcripts import TranscriptCursor, TranscriptPage


@pytest.mark.asyncio
async def test_toad_public_types_and_acp_agent_comms_metadata(tmp_path) -> None:
    assert all(
        isinstance(public_type, type)
        for public_type in (Comms, Goal, TranscriptCursor, TranscriptPage, MessageRoute)
    )
    comms = wire(tmp_path / "wire")
    assert isinstance(comms, Comms)
    comms.registry.declare(comms.registry.new_thread('worker', frozenset(), str(tmp_path)))
    agent = CommsAgent(comms, agent_bin="nonexistent-pi", runtime_enabled=False, auto_wake=False)

    session = NewSessionResponse(
        session_id="worker", field_meta=(await agent.sessions.metadata("worker"))
    ).model_dump(by_alias=True, exclude_none=True)
    configuration = next(update for update in decode_updates(session["_meta"])
                         if isinstance(update, CoordinationChangedUpdate))
    assert configuration.thread == comms.registry.require("worker").incarnation
    assert configuration.thread.name == "worker"
    assert configuration.wire_root == str(comms.root.resolve())

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
    assert decode_updates(update["_meta"]) == (TextRouteUpdate(route),)
    await agent.shutdown()
