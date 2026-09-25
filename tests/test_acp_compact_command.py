"""No-provider ACP routing tests for explicit saved-session /compact.

These tests use a fake bridge and assert the command never reaches an agent turn.
The runner's separate fake-provider tests establish no-retry and session safety.
"""

from __future__ import annotations

import sys
import types

import pytest
from acp import RequestError
from acp.agent.router import build_agent_router
from acp.schema import TextContentBlock

from agent_comms.acp import CommsAgent
from agent_comms.operations import wire


def block(text: str) -> TextContentBlock:
    return TextContentBlock(type="text", text=text)


@pytest.mark.asyncio
async def test_compact_is_exclusive_bridge_command_not_model_prompt(tmp_path, monkeypatch):
    agent = CommsAgent(wire(tmp_path / "wire"), auto_wake=False)
    session = (await agent.new_session(cwd=str(tmp_path / "work"))).session_id
    calls = []
    bridge = types.ModuleType("agent_comms.manual_compaction_bridge")

    async def compact_context(owner, session_id, instructions):
        calls.append((owner, session_id, instructions))
        return {"ok": True, "status": "compacted"}

    async def forbidden_model(*args, **kwargs):
        raise AssertionError("/compact must not become a model prompt")

    bridge.compact_context = compact_context
    monkeypatch.setitem(sys.modules, bridge.__name__, bridge)
    monkeypatch.setattr(agent, "_run_agent_turn", forbidden_model)
    try:
        response = await agent.prompt(session, [block("/compact   focus on safety ")])
        assert response.stop_reason == "end_turn"
        assert response.field_meta == {
            "agentComms": {"compaction": {"ok": True, "status": "compacted"}}
        }
        assert calls == [(agent, session, "focus on safety")]
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_toad_blank_prompt_metadata_compacts_without_model(tmp_path, monkeypatch):
    agent = CommsAgent(wire(tmp_path / "wire"), auto_wake=False)
    session = (await agent.new_session(cwd=str(tmp_path / "work"))).session_id
    calls = []
    bridge = types.ModuleType("agent_comms.manual_compaction_bridge")

    async def compact_context(owner, session_id, instructions):
        calls.append((owner, session_id, instructions))
        return {"ok": True, "status": "compacted"}

    async def forbidden_model(*args, **kwargs):
        raise AssertionError("Toad's compact metadata must not be a model prompt")

    bridge.compact_context = compact_context
    monkeypatch.setitem(sys.modules, bridge.__name__, bridge)
    monkeypatch.setattr(agent, "_run_agent_turn", forbidden_model)
    try:
        response = await agent.prompt(
            session, [block(" ")], agentComms={"compact": "focus on safety"}
        )
        assert response.stop_reason == "end_turn"
        assert response.field_meta == {
            "agentComms": {"compaction": {"ok": True, "status": "compacted"}}
        }
        assert calls == [(agent, session, "focus on safety")]
        with pytest.raises(RequestError) as invalid:
            await agent.prompt(session, [block("/compact")], agentComms={"compact": None})
        assert invalid.value.data == {"reason": "Compaction metadata requires a blank text block."}
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_sdk_router_preserves_toad_wire_metadata(tmp_path, monkeypatch):
    agent = CommsAgent(wire(tmp_path / "wire"), auto_wake=False)
    session = (await agent.new_session(cwd=str(tmp_path / "work"))).session_id
    calls = []
    bridge = types.ModuleType("agent_comms.manual_compaction_bridge")

    async def compact_context(owner, session_id, instructions):
        calls.append((owner, session_id, instructions))
        return {"ok": True, "status": "compacted"}

    bridge.compact_context = compact_context
    monkeypatch.setitem(sys.modules, bridge.__name__, bridge)
    try:
        response = await build_agent_router(agent)(
            "session/prompt",
            {
                "sessionId": session,
                "prompt": [{"type": "text", "text": " "}],
                "_meta": {"agentComms": {"compact": "focus"}},
            },
            False,
        )
        assert response.model_dump(by_alias=True, exclude_none=True) == {
            "stopReason": "end_turn",
            "_meta": {"agentComms": {"compaction": {"ok": True, "status": "compacted"}}},
        }
        assert calls == [(agent, session, "focus")]
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_compact_failure_is_not_end_turn_success(tmp_path, monkeypatch):
    agent = CommsAgent(wire(tmp_path / "wire"), auto_wake=False)
    session = (await agent.new_session(cwd=str(tmp_path / "work"))).session_id
    bridge = types.ModuleType("agent_comms.manual_compaction_bridge")

    async def compact_context(*args):
        return {"ok": False, "error": "uncertain compaction"}

    bridge.compact_context = compact_context
    monkeypatch.setitem(sys.modules, bridge.__name__, bridge)
    try:
        with pytest.raises(RequestError) as failure:
            await agent.prompt(session, [block("/compact")])
        assert failure.value.data == {"reason": "uncertain compaction"}
        assert str(failure.value) == "uncertain compaction"
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_attached_compact_routes_to_owner_not_attached_model(tmp_path):
    agent = CommsAgent(wire(tmp_path / "wire"), auto_wake=False)
    calls = []

    class Proxy:
        async def request(self, action, **kwargs):
            calls.append((action, kwargs))
            return {"ok": True, "status": "compacted"}

    agent._proxies["attached"] = Proxy()
    response = await agent.prompt("attached", [block(" ")], agentComms={"compact": None})
    assert response.stop_reason == "end_turn"
    assert response.field_meta == {
        "agentComms": {"compaction": {"ok": True, "status": "compacted"}}
    }
    assert calls == [("compact", {"instructions": ""})]


@pytest.mark.asyncio
async def test_compact_rejects_multimodal_and_overlong_without_bridge_or_model(tmp_path):
    agent = CommsAgent(wire(tmp_path / "wire"), auto_wake=False)
    session = (await agent.new_session(cwd=str(tmp_path / "work"))).session_id
    try:
        with pytest.raises(RequestError) as multimodal:
            await agent.prompt(session, [block("/compact"), {"type": "image", "data": "abc"}])
        assert multimodal.value.data == {"reason": "/compact requires one text block."}
        with pytest.raises(RequestError) as oversized:
            await agent.prompt(session, [block("/compact " + "x" * 2001)])
        assert oversized.value.data == {"reason": "Compaction instructions are too long."}
    finally:
        await agent.shutdown()
