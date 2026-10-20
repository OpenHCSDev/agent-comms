import asyncio

import agent_comms.agent_events as ae
import agent_comms.backend as backend
from agent_comms.acp_extension import (
    InputStartedUpdate,
    QueueChangedUpdate,
    QueuePromptRequest,
    SteerPromptRequest,
    decode_updates,
    encode_request,
)
from agent_comms.comms import wire
from delivery_owner_fixture import canonical_agent
from conftest import await_event_or_task


async def make_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/base")
    agent = canonical_agent(wire(tmp_path), agent_bin="pi", agent_args=["--model", "test/base"])
    await agent.new_session(str(tmp_path / "project"))
    return agent


async def test_cancellation_restores_unprocessed_user_queue(tmp_path, monkeypatch):
    agent = await make_agent(tmp_path, monkeypatch)
    active = asyncio.Event()
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"].model_dump(by_alias=True, exclude_none=True))

    agent.on_connect(Client())

    async def events(*args, **kwargs):
        active.set()
        await asyncio.sleep(60)
        yield ae.Done(ok=True, text="")

    monkeypatch.setattr(backend, "stream_agent_events", events)
    turn = asyncio.create_task(agent.prompt("project", [{"type": "text", "text": "original"}]))
    try:
        async with asyncio.timeout(3):
            await await_event_or_task(active.wait(), turn)
        await agent.prompt(
            "project",
            [{"type": "text", "text": "keep this queued text"}],
            field_meta=encode_request(QueuePromptRequest(None, True)),
        )
        await agent.cancel("project")
        assert (await turn).stop_reason == "cancelled"
        assert any(
            [row.text for row in fact.projection.restored] == ["keep this queued text"]
            for u in updates
            for fact in decode_updates(u.get("_meta"))
            if isinstance(fact, QueueChangedUpdate)
        )
    finally:
        await agent.shutdown()
