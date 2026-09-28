import asyncio

from agent_comms import agent_events as ae
from agent_comms import backend
from agent_comms.acp import CommsAgent
from agent_comms.comms import wire


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
        yield ae.Chunk(text="Original response")
        queue = kwargs["steering_queue"]
        commands.append(await queue.get())
        commands.append(await queue.get())
        yield ae.InputStarted(id=commands[1]["_input_id"])
        yield ae.InputStarted(id=commands[0]["_input_id"])
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="")

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
        assert len(agent.inputs.queued_inputs["project"]) == 1
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
        await active.wait()
        await agent.prompt(
            "project",
            [{"type": "text", "text": "keep this queued text"}],
            field_meta={"agentComms": {"deferDisplay": True}},
        )
        await agent.cancel("project")
        assert (await turn).stop_reason == "cancelled"
        assert any(
            [
                row["text"]
                for row in (u.get("_meta", {}).get("agentComms", {}).get("queueState") or {}).get(
                    "restored", []
                )
            ]
            == ["keep this queued text"]
            for u in updates
        )
    finally:
        await agent.shutdown()
