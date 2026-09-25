"""Goal admission uses each follow-up's authority, including goal origin turns.

The backend event source is controlled here to exercise exact admission races.
The callbacks, private goal ledger, ACP prompt queue, and transcript replay are real.
"""

import json

import pytest
from acp.schema import TextContentBlock

from agent_comms.acp import CommsAgent
from agent_comms.input_disposition import InputDispositions
from agent_comms.operations import wire


async def owner(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    monkeypatch.setattr(agent, "_schedule_wake", lambda _session: None)
    updates = []

    class Client:
        async def session_update(self, session_id, update):
            updates.append(update)

    agent.on_connect(Client())
    await agent.new_session(str(tmp_path / "project"))
    session = tmp_path / "session.jsonl"
    session.touch()
    comms.attach_session("project", str(session))
    return agent, comms, session, updates


def persist_user(session, native_id, text):
    with session.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "message",
                    "id": native_id[:8],
                    "message": {
                        "role": "user",
                        "inputId": native_id,
                        "content": [{"type": "text", "text": text}],
                    },
                }
            )
            + "\n"
        )


async def queue_followup(agent, kwargs):
    response = await agent.prompt(
        "project",
        [TextContentBlock(type="text", text="follow-up for model")],
        agentComms={"userText": "follow-up as typed", "deferDisplay": True},
    )
    public_id = response.field_meta["agentComms"]["inputDisposition"]["inputId"]
    command = kwargs["steering_queue"].get_nowait()
    assert command["_input_id"] == public_id
    return public_id, command


async def replay(agent):
    updates = []

    class Client:
        transcript_snapshots = True

        async def session_update(self, session_id, update):
            updates.append(update)

    await agent._replay_transcript("project", "project", client=Client())
    return [item["text"] for item in updates[0].field_meta["agentComms"]["transcript"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("queued_before_activation", [False, True])
async def test_origin_goal_allows_only_followup_admitted_after_activation(
    tmp_path, monkeypatch, queued_before_activation
):
    agent, comms, session, _ = await owner(tmp_path, monkeypatch)
    observed = {}

    async def events(*args, **kwargs):
        with kwargs["send_boundary"](None, "a" * 32, args[2]) as allowed:
            assert allowed is True
        persist_user(session, "a" * 32, args[2])
        assert kwargs["native_start"](None, "a" * 32, args[2])
        yield {"type": "input_started", "id": None}
        if queued_before_activation:
            public_id, command = await queue_followup(agent, kwargs)
        goal = comms.update_goal("project", "set", text="Read files until stopped")
        yield {"type": "tool_end", "id": "set-goal", "name": "comms_set_goal", "ok": True}
        assert agent._goal_store.snapshot(goal.id).state == "reserved"
        if not queued_before_activation:
            public_id, command = await queue_followup(agent, kwargs)
        observed.update(goal=goal, public_id=public_id)
        with kwargs["send_boundary"](public_id, "b" * 32, command["message"]) as allowed:
            assert allowed is (None if queued_before_activation else True)
        if queued_before_activation:
            yield {"type": "input_refused", "id": public_id}
        else:
            persist_user(session, "b" * 32, command["message"])
            assert kwargs["native_start"](public_id, "b" * 32, command["message"])
            yield {"type": "input_started", "id": public_id}
        yield {"type": "chunk", "text": "Goal set and work completed this turn."}
        yield {"type": "settled"}
        yield {"type": "done", "ok": True, "text": "Goal set and work completed this turn."}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent._run_agent_turn(
            "project", "project", "Set a goal for model", initial_display_text="Set a goal as typed"
        )
        assert comms.registry.require("project").goal.active
        state = agent._goal_store.snapshot(observed["goal"].id)
        assert state.state == "ready" and state.number == 2
        key = "acp:" + observed["public_id"]
        assert InputDispositions(comms.root).status(key) == (
            "unknown" if queued_before_activation else "started"
        )
        assert await replay(agent) == ["Set a goal as typed"] + (
            [] if queued_before_activation else ["follow-up as typed"]
        )
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "clear", "replace"])
async def test_autonomous_goal_followup_checks_current_goal_and_hides_internal_prompt(
    tmp_path, monkeypatch, change
):
    agent, comms, session, _ = await owner(tmp_path, monkeypatch)
    store = agent._open_goal_store()
    original_goal = comms.update_goal(
        "project", "set", text="Read files until stopped", owner_store=store
    )
    observed = {}

    async def events(*args, **kwargs):
        assert "Persistent goal" in args[2]
        with kwargs["send_boundary"](None, "a" * 32, args[2]) as allowed:
            assert allowed is True
        persist_user(session, "a" * 32, args[2])
        assert kwargs["native_start"](None, "a" * 32, args[2])
        yield {"type": "input_started", "id": None}
        public_id, command = await queue_followup(agent, kwargs)
        observed["public_id"] = public_id
        if change == "clear":
            comms.update_goal("project", "clear")
        elif change == "replace":
            observed["replacement"] = comms.update_goal(
                "project", "set", text="A different goal", owner_store=store
            )
        with kwargs["send_boundary"](public_id, "b" * 32, command["message"]) as allowed:
            assert allowed is (change is None)
        if change is None:
            persist_user(session, "b" * 32, command["message"])
            assert kwargs["native_start"](public_id, "b" * 32, command["message"])
            yield {"type": "input_started", "id": public_id}
        else:
            yield {"type": "input_refused", "id": public_id}
        yield {"type": "chunk", "text": "Finished reading this section."}
        yield {"type": "settled"}
        yield {"type": "done", "ok": change is None, "text": "Finished reading this section."}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent._run_agent_turn(
            "project", "project", "Continue working toward the active goal.", autonomous_goal=True
        )
        key = "acp:" + observed["public_id"]
        assert InputDispositions(comms.root).status(key) == (
            "started" if change is None else "unknown"
        )
        assert await replay(agent) == (["follow-up as typed"] if change is None else [])
        current = comms.registry.require("project").goal
        if change is None:
            assert current.active and current.id == original_goal.id
            assert store.snapshot(current.id).state == "ready"
        elif change == "clear":
            assert current is None
        else:
            assert current.active and current.id == observed["replacement"].id
            assert store.snapshot(current.id).state == "ready"
    finally:
        await agent.shutdown()
