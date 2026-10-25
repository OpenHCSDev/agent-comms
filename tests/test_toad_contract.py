"""Public agent-comms and ACP fields consumed by pinned Toad builds."""
from agent_comms.repo import Checkout
from agent_comms.controller import SessionUpdateSink

from functools import partial

import pytest
from acp.schema import NewSessionResponse

from agent_comms.acp import CommsAgent
from agent_comms.agent_event_updates import AcpEventConsumer, Routed, RoutedTurnEventConsumer
from agent_comms.agent_events import Chunk
from agent_comms.acp_extension import CoordinationChangedUpdate, TextRouteUpdate, decode_updates
from agent_comms.comms import Comms, wire
from agent_comms.goals import Goal
from agent_comms.field_codec import TurnId
from agent_comms.routing import MessageRoute
from agent_comms.threads import Thread
from agent_comms.transcripts import TranscriptCursor, TranscriptPage
from agent_comms.native_arguments import GivenBinary


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", (
    lambda turn_id: Routed,
    lambda turn_id: partial(RoutedTurnEventConsumer, turn_id=turn_id),
))
async def test_toad_public_types_and_acp_agent_comms_metadata(tmp_path, consumer) -> None:
    turn_id = TurnId.mint()
    assert all(
        isinstance(public_type, type)
        for public_type in (Comms, Goal, TranscriptCursor, TranscriptPage, MessageRoute)
    )
    comms = wire(tmp_path / "wire")
    assert isinstance(comms, Comms)
    comms.registry.declare(Thread('worker', frozenset(), Checkout(tmp_path)))
    agent = CommsAgent(comms, agent_bin=GivenBinary("nonexistent-pi"), runtime_enabled=False, auto_wake=False)

    session = NewSessionResponse(
        session_id="worker", field_meta=(await agent.sessions.metadata("worker"))
    ).model_dump(by_alias=True, exclude_none=True)
    configuration = next(update for update in decode_updates(session["_meta"])
                         if isinstance(update, CoordinationChangedUpdate))
    assert configuration.thread == comms.registry.require("worker").incarnation
    assert configuration.thread.name == "worker"
    assert configuration.wire_root == str(comms.root.resolve())

    updates = []

    class Client(SessionUpdateSink):
        async def publish_update(self, session_id, update):
            assert session_id == "worker"
            updates.append(update.model_dump(by_alias=True, exclude_none=True))
            return True

    route = MessageRoute("worker", ("#team",))
    client = Client()
    await consumer(turn_id)(
        agent=agent, session_id="worker", client=client, route=route
    ).dispatch(Chunk(text="routed reply"))
    assert len(updates) == 1
    update = updates[0]
    assert update["content"]["text"] == "routed reply"
    assert decode_updates(update["_meta"]) == (TextRouteUpdate(route),)
    await AcpEventConsumer(agent, "worker", client).dispatch(Chunk(text="unrouted reply"))
    assert len(updates) == 2
    assert updates[1]["content"]["text"] == "unrouted reply"
    assert "_meta" not in updates[1]
    await agent.shutdown()
