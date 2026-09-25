"""Original owner inputs use the goal authority captured at local admission.

These tests control backend timing, exercising the real admission callbacks,
private goal ledger, and durable UNKNOWN/STARTED transition without a provider.
"""

import json

import pytest
from acp import RequestError

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


def disposition_rows(agent):
    return list(agent._dispositions._read().values())


@pytest.mark.asyncio
async def test_idle_owner_original_input_continues_active_goal(tmp_path, monkeypatch):
    agent, comms, session, _ = await owner(tmp_path, monkeypatch)
    store = agent._open_goal_store()
    goal = comms.update_goal("project", "set", text="Keep reading", owner_store=store)

    async def events(*args, **kwargs):
        native_id = "a" * 32
        with kwargs["send_boundary"](None, native_id, args[2]) as allowed:
            assert allowed is True
        session.write_text(
            json.dumps(
                {
                    "type": "message",
                    "id": "owner-input",
                    "message": {
                        "role": "user",
                        "inputId": native_id,
                        "content": [{"type": "text", "text": args[2]}],
                    },
                }
            )
            + "\n"
        )
        assert kwargs["native_start"](None, native_id, args[2])
        yield {"type": "input_started", "id": None}
        yield {"type": "chunk", "text": "Read the requested file."}
        yield {"type": "settled"}
        yield {"type": "done", "ok": True, "text": "Read the requested file."}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent._run_owned_input("project", "project", "testing steering")
        rows = disposition_rows(agent)
        assert len(rows) == 1 and rows[0]["status"] == "started"
        current = comms.registry.require("project").goal
        assert current.id == goal.id and current.active
        generation = store.snapshot(goal.id)
        assert generation.state == "ready" and generation.number == 2
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_goal", [False, True], ids=["activated", "replaced"])
async def test_changed_goal_before_original_turn_does_not_consume_new_grant(
    tmp_path, monkeypatch, existing_goal
):
    agent, comms, _, _ = await owner(tmp_path, monkeypatch)
    store = agent._open_goal_store()
    if existing_goal:
        comms.update_goal("project", "set", text="Original goal", owner_store=store)
    original_emit = agent._emit_input_disposition
    replacement = None
    backend_calls = 0

    async def activate_after_admission(session_id, row):
        nonlocal replacement
        await original_emit(session_id, row)
        if replacement is None:
            replacement = comms.update_goal(
                "project", "set", text="New authority", owner_store=store
            )

    async def events(*args, **kwargs):
        nonlocal backend_calls
        backend_calls += 1
        yield {"type": "done", "ok": False, "text": "Must not reach backend"}

    monkeypatch.setattr(agent, "_emit_input_disposition", activate_after_admission)
    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        with pytest.raises(RequestError) as refused:
            await agent._run_owned_input("project", "project", "admitted before goal change")
        assert refused.value.data == {"reason": "input_authority_changed"}
        assert backend_calls == 0
        rows = disposition_rows(agent)
        assert len(rows) == 1 and rows[0]["status"] == "unknown"
        assert rows[0]["native_id"] is None
        current = comms.registry.require("project").goal
        assert current.id == replacement.id and current.active
        generation = store.snapshot(replacement.id)
        assert generation.state == "ready" and generation.number == 1
        reopened = InputDispositions(comms.root)
        assert len(reopened.unknown(frozenset({"project"}))) == 1
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_original_goal_input_cannot_send_after_owner_stops(tmp_path, monkeypatch):
    agent, comms, _, _ = await owner(tmp_path, monkeypatch)
    store = agent._open_goal_store()
    goal = comms.update_goal("project", "set", text="Keep reading", owner_store=store)
    boundaries = []

    async def events(*args, **kwargs):
        comms.registry.unregister("project")
        with kwargs["send_boundary"](None, "b" * 32, args[2]) as allowed:
            boundaries.append(allowed)
        yield {"type": "done", "ok": False, "text": "Owner stopped before send"}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent._run_owned_input("project", "project", "do not send after stop")
        assert boundaries == [False]
        rows = disposition_rows(agent)
        assert len(rows) == 1 and rows[0]["status"] == "unknown"
        assert rows[0]["native_id"] is None
        assert store.snapshot(goal.id).state != "ready"
    finally:
        await agent.shutdown()
