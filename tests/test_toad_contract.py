"""Public agent-comms and ACP fields consumed by pinned Toad builds."""
from functools import partial

import pytest
from acp.schema import NewSessionResponse

from agent_comms.execution.acp import CommsAgent
from agent_comms.acp_extension import CoordinationChangedUpdate, TextRouteUpdate, decode_updates
from agent_comms.agent_event_updates import AcpEventConsumer, Routed, RoutedTurnEventConsumer
from agent_comms.agent_events import Chunk
from agent_comms.comms import wire
from agent_comms.execution.controller import SessionUpdateSink
from agent_comms.execution.native_arguments import GivenBinary
from agent_comms.messaging.routing import MessageRoute
from agent_comms.nominal.field_codec import AcpSessionId, TurnId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", (
    lambda turn_id: Routed,
    lambda turn_id: partial(RoutedTurnEventConsumer, turn_id=turn_id),
))
async def test_toad_public_types_and_acp_agent_comms_metadata(tmp_path, consumer) -> None:
    turn_id = TurnId.mint()
    comms = wire(tmp_path / "wire")
    comms.registry.declare(NativeThread('worker', frozenset(), Checkout(tmp_path)))
    agent = CommsAgent(comms, agent_bin=GivenBinary("nonexistent-pi"), auto_wake=False)

    session_id = AcpSessionId("worker")
    session = NewSessionResponse(
        session_id=str(session_id), field_meta=(await agent.sessions.metadata("worker", session_id=session_id))
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
    # An unrouted reply carries no agent-comms fact. An empty batch decodes to the same () as none.
    assert decode_updates(updates[1].get("_meta")) == ()
    await agent.shutdown()
