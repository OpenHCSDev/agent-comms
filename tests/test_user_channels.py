import asyncio

import pytest

from agent_comms import ThreadRole, wire
from agent_comms.acp import CommsAgent


def _native_receipt(args, kwargs):
    native_id = "a" * 32
    with kwargs["send_boundary"](None, native_id, args[2]) as allowed:
        assert allowed is True
    assert kwargs["native_start"](None, native_id, args[2])


async def test_user_channel_wakes_members_and_returns_answers_without_feedback(
    tmp_path, monkeypatch
):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", agent_args=[], runtime_enabled=True)
    await agent.new_session(str(tmp_path / "first"))
    await agent.new_session(str(tmp_path / "second"))
    for name in ("first", "second"):
        comms.update_tags(name, add=frozenset({"openhcs"}))
    calls = []

    async def events(*args, **kwargs):
        _native_receipt(args, kwargs)
        name = args[4]["AGENT_COMMS_THREAD"]
        calls.append(name)
        yield {"type": "chunk", "text": f"{name} received it"}
        yield {"type": "settled"}
        yield {"type": "done", "ok": True}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        comms.send_user_message("#openhcs", "anyone receive this?", worktree=str(tmp_path))
        for name in ("first", "second"):
            await agent._drain_inbox(name)
        await asyncio.gather(
            *(asyncio.wait_for(agent._wake_tasks[name], 2) for name in ("first", "second"))
        )
        messages = comms.channel_history("#openhcs")
        assert len([message for message in messages if message.membership is not None]) == 2
        messages = [message for message in messages if message.membership is None]
        assert messages[0].sender == "user"
        assert messages[0].sender_role is ThreadRole.USER
        assert {message.body for message in messages[1:]} == {
            "first received it",
            "second received it",
        }
        for name in ("first", "second"):
            await agent._drain_inbox(name)
        await asyncio.sleep(0.05)
        # Agent-authored unmentioned channel replies remain informational and
        # do not recursively wake peers.
        assert sorted(calls) == ["first", "second"]
        assert len(comms.channel_history("#openhcs")) == 5
        assert all(view.thread.role.executable for view in comms.thread_views())
        assert comms.user_identity(str(tmp_path)).name == "user"
        assert comms.registry.require("user").pid == 0
        with pytest.raises(ValueError, match="executor"):
            comms.acquire_thread("user", owner_pid=1234)
        with pytest.raises(ValueError, match="user-message"):
            comms.send("user", "#openhcs", "impersonated")
    finally:
        await agent.shutdown()


async def test_channel_requests_keep_their_reply_destinations(tmp_path, monkeypatch):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", agent_args=[], runtime_enabled=True)
    await agent.new_session(str(tmp_path / "worker"))
    comms.update_tags("worker", add=frozenset({"first", "second"}))

    async def events(*args, **kwargs):
        _native_receipt(args, kwargs)
        yield {"type": "chunk", "text": "Reply: " + args[2].splitlines()[-1]}
        yield {"type": "settled"}
        yield {"type": "done", "ok": True}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        comms.send_user_message("#first", "first request", worktree=str(tmp_path))
        comms.send_user_message("#second", "second request", worktree=str(tmp_path))
        await agent._drain_inbox("worker")
        await asyncio.wait_for(agent._wake_tasks["worker"], 2)
        assert comms.channel_history("#first")[-1].body == "Reply: first request"
        assert comms.channel_history("#second")[-1].body == "Reply: second request"
    finally:
        await agent.shutdown()


async def test_human_channel_mention_wakes_only_named_member(tmp_path, monkeypatch):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", agent_args=[], runtime_enabled=True)
    await agent.new_session(str(tmp_path / "alpha"))
    await agent.new_session(str(tmp_path / "beta"))
    for name in ("alpha", "beta"):
        comms.update_tags(name, add=frozenset({"team"}))
    calls: list[str] = []

    async def events(*args, **kwargs):
        _native_receipt(args, kwargs)
        calls.append(args[4]["AGENT_COMMS_THREAD"])
        yield {"type": "settled"}
        yield {"type": "done", "ok": True}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        comms.send_user_message("#team", "@beta please answer", worktree=str(tmp_path))
        for name in ("alpha", "beta"):
            await agent._drain_inbox(name)
        await asyncio.wait_for(agent._wake_tasks["beta"], 2)
        await asyncio.sleep(0.05)
        assert calls == ["beta"]
    finally:
        await agent.shutdown()


async def test_agent_channel_mention_wakes_only_named_member(tmp_path, monkeypatch):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", agent_args=[], runtime_enabled=True)
    await agent.new_session(str(tmp_path / "alpha"))
    await agent.new_session(str(tmp_path / "beta"))
    for name in ("alpha", "beta"):
        comms.update_tags(name, add=frozenset({"team"}))
    calls: list[tuple[str, str]] = []

    async def events(*args, **kwargs):
        _native_receipt(args, kwargs)
        calls.append((args[4]["AGENT_COMMS_THREAD"], args[2]))
        yield {"type": "settled"}
        yield {"type": "done", "ok": True}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        comms.send("alpha", "#team", "@beta please investigate")
        for name in ("alpha", "beta"):
            await agent._drain_inbox(name)
        await asyncio.wait_for(agent._wake_tasks["beta"], 2)
        assert [name for name, _ in calls] == ["beta"]
        assert "Response policy: mentioned_only" in calls[0][1]
        assert "only resolved mentioned identities may respond: @beta" in calls[0][1]
        assert "delivery and history remain #team" in calls[0][1]
        assert comms.pending_count("beta") == 0

        # Unmentioned agent-authored channel chatter remains informational.
        comms.send("alpha", "#team", "status update")
        for name in ("alpha", "beta"):
            await agent._drain_inbox(name)
        await asyncio.sleep(0.05)
        assert [name for name, _ in calls] == ["beta"]
    finally:
        await agent.shutdown()
