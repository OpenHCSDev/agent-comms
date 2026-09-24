import asyncio

from agent_comms import backend, wire
from agent_comms.acp import CommsAgent


async def make_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/base")
    agent = CommsAgent(wire(tmp_path), agent_bin="pi", agent_args=["--model", "test/base"])
    await agent.new_session("/tmp/project")
    return agent


async def test_queued_and_steered_followups_both_reach_the_next_boundary(tmp_path, monkeypatch):
    agent = await make_agent(tmp_path, monkeypatch)
    active = asyncio.Event()
    commands, updates = [], []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"].model_dump(by_alias=True, exclude_none=True))

    agent.on_connect(Client())

    async def events(*args, **kwargs):
        active.set()
        yield {"type": "chunk", "text": "Original response"}
        queue = kwargs["steering_queue"]
        commands.append(await queue.get())
        commands.append(await queue.get())
        yield {"type": "input_started", "id": commands[1]["_input_id"]}
        yield {"type": "input_started", "id": commands[0]["_input_id"]}
        yield {"type": "settled"}
        yield {"type": "done", "ok": True}

    monkeypatch.setattr(backend, "stream_agent_events", events)
    turn = asyncio.create_task(agent.prompt("project", [{"type": "text", "text": "original"}]))
    try:
        await active.wait()
        await agent.prompt(
            "project",
            [{"type": "text", "text": "later"}],
            field_meta={"agentComms": {"deferDisplay": True, "userText": "later"}},
        )
        assert not turn.done()
        assert len(agent._queued_inputs["project"]) == 1
        await agent.prompt(
            "project",
            [{"type": "text", "text": "now"}],
            agentComms={"delivery": "steer"},  # ACP SDK flattens _meta into kwargs.
        )
        await asyncio.wait_for(turn, timeout=2)
        # Both deliveries steer at the next boundary; only local echo is deferred.
        assert [command["streamingBehavior"] for command in commands] == ["steer", "steer"]
        starts = [
            u["_meta"]["agentComms"]["inputStarted"]["text"]
            for u in updates
            if "inputStarted" in u.get("_meta", {}).get("agentComms", {})
        ]
        assert starts == [None, "later"]
        assert not agent._queued_inputs.get("project")
    finally:
        await agent.shutdown()
