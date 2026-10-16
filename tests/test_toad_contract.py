"""Public agent-comms and ACP fields consumed by pinned Toad builds."""

from dataclasses import asdict

import pytest
from acp.schema import NewSessionResponse

from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.goals import Goal
from agent_comms.routing import MessageRoute
from agent_comms.threads import Thread
from agent_comms.transcripts import TranscriptCursor, TranscriptPage
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.active_route import resolve_comms_route
import dataclasses
from agent_comms.launch_runtime import LaunchRuntime


@pytest.mark.asyncio
async def test_toad_public_types_and_acp_agent_comms_metadata(tmp_path) -> None:
    assert all(
        isinstance(public_type, type)
        for public_type in (Comms, Goal, TranscriptCursor, TranscriptPage, MessageRoute)
    )
    comms = Comms(resolve_comms_route(tmp_path / "wire"), runtime=LaunchRuntime.current())
    assert isinstance(comms, Comms)
    comms.registry.declare(Thread("worker", frozenset(), str(tmp_path)))
    comms.settings = dataclasses.replace(comms.settings)
    agent = CommsAgent(comms,  runtime_enabled=False, auto_wake=False)

    session = NewSessionResponse(
        session_id="worker", field_meta=(await agent.sessions.metadata("worker"))
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
