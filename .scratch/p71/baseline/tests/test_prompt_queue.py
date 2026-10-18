import asyncio

from agent_comms import agent_events as ae
from agent_comms import backend
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


async def make_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/base")
    agent = canonical_agent(wire(tmp_path), agent_bin="pi", agent_args=["--model", "test/base"])
    await agent.new_session(str(tmp_path / "project"))
    return agent


async def test_queued_and_steered_followups_both_reach_the_next_boundary(tmp_path, monkeypatch):
    agent = await make_agent(tmp_path, monkeypatch)
    active = asyncio.Event()
    commands, updates = ([], [])

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"].model_dump(by_alias=True, exclude_none=True))

    agent.on_connect(Client())

    async def events(*args, **kwargs):
        active.set()
        yield ae.Chunk(text="Original response")
        queue = kwargs["steering_queue"]
        commands.append(await queue.get())
        commands.append(await queue.get())
        yield ae.InputStarted(id=commands[1].input_id.value)
        yield ae.InputStarted(id=commands[0].input_id.value)
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="")

    monkeypatch.setattr(backend, "stream_agent_events", events)
    turn = asyncio.create_task(agent.prompt("project", [{"type": "text", "text": "original"}]))
    try:
        await asyncio.wait_for(active.wait(), 3)
        await agent.prompt(
            "project",
            [{"type": "text", "text": "later"}],
            field_meta=encode_request(QueuePromptRequest("later", True)),
        )
        assert not turn.done()
        assert len(agent.inputs.queued_inputs["project"]) == 1
        await agent.prompt(
            "project",
            [{"type": "text", "text": "now"}],
            agentComms=encode_request(SteerPromptRequest(None, False))["agentComms"],
        )
        await asyncio.wait_for(turn, timeout=2)
        assert [command["streamingBehavior"] for command in commands] == ["steer", "steer"]
        starts = [
            fact.text
            for u in updates
            for fact in decode_updates(u.get("_meta"))
            if isinstance(fact, InputStartedUpdate)
        ]
        assert starts == [None, "later"]
        assert not agent.inputs.queued_inputs.get("project")
    finally:
        await agent.shutdown()


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
        await asyncio.wait_for(active.wait(), 3)
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
