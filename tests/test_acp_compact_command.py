"""No-provider ACP routing tests for explicit saved-session /compact.

These tests use a fake bridge and assert the command never reaches an agent turn.
The runner's separate fake-provider tests establish no-retry and session safety.
"""

from __future__ import annotations

import os

import pytest
from acp import RequestError
from acp.agent.router import build_agent_router
from acp.schema import TextContentBlock

from agent_comms.acp import CommsAgent
from agent_comms.comms import wire
from delivery_owner_fixture import canonical_agent


def block(text: str) -> TextContentBlock:
    return TextContentBlock(type="text", text=text)


@pytest.mark.asyncio
async def test_compact_is_exclusive_bridge_command_not_model_prompt(tmp_path, monkeypatch):
    agent = canonical_agent(wire(tmp_path / "wire"), auto_wake=False)
    session = (await agent.new_session(cwd=str(tmp_path / "work"))).session_id
    calls = []

    async def compact_context(owner, session_id, instructions):
        calls.append((owner, session_id, instructions))
        return {"ok": True, "status": "compacted"}

    async def forbidden_model(*args, **kwargs):
        raise AssertionError("/compact must not become a model prompt")

    monkeypatch.setattr("agent_comms.manual_compaction_bridge.compact_context", compact_context)
    monkeypatch.setattr(agent.turns, "run_agent_turn", forbidden_model)
    try:
        response = await agent.prompt(session, [block("/compact   focus on safety ")])
        assert response.stop_reason == "end_turn"
        assert response.field_meta == {
            "agentComms": {"compaction": {"ok": True, "status": "compacted"}}
        }
        assert calls == [(agent.turns, session, "focus on safety")]
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_toad_blank_prompt_metadata_compacts_without_model(tmp_path, monkeypatch):
    agent = canonical_agent(wire(tmp_path / "wire"), auto_wake=False)
    session = (await agent.new_session(cwd=str(tmp_path / "work"))).session_id
    calls = []

    async def compact_context(owner, session_id, instructions):
        calls.append((owner, session_id, instructions))
        return {"ok": True, "status": "compacted"}

    async def forbidden_model(*args, **kwargs):
        raise AssertionError("Toad's compact metadata must not be a model prompt")

    monkeypatch.setattr("agent_comms.manual_compaction_bridge.compact_context", compact_context)
    monkeypatch.setattr(agent.turns, "run_agent_turn", forbidden_model)
    try:
        response = await agent.prompt(
            session, [block(" ")], agentComms={"compact": "focus on safety"}
        )
        assert response.stop_reason == "end_turn"
        assert response.field_meta == {
            "agentComms": {"compaction": {"ok": True, "status": "compacted"}}
        }
        assert calls == [(agent.turns, session, "focus on safety")]
        with pytest.raises(RequestError) as invalid:
            await agent.prompt(session, [block("/compact")], agentComms={"compact": None})
        assert invalid.value.data == {"reason": "Compaction metadata requires a blank text block."}
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_sdk_router_preserves_toad_wire_metadata(tmp_path, monkeypatch):
    agent = canonical_agent(wire(tmp_path / "wire"), auto_wake=False)
    session = (await agent.new_session(cwd=str(tmp_path / "work"))).session_id
    calls = []

    async def compact_context(owner, session_id, instructions):
        calls.append((owner, session_id, instructions))
        return {"ok": True, "status": "compacted"}

    monkeypatch.setattr("agent_comms.manual_compaction_bridge.compact_context", compact_context)
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
        assert calls == [(agent.turns, session, "focus")]
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_compact_failure_is_not_end_turn_success(tmp_path, monkeypatch):
    agent = canonical_agent(wire(tmp_path / "wire"), auto_wake=False)
    session = (await agent.new_session(cwd=str(tmp_path / "work"))).session_id

    async def compact_context(*args):
        return {"ok": False, "error": "uncertain compaction"}

    monkeypatch.setattr("agent_comms.manual_compaction_bridge.compact_context", compact_context)
    try:
        with pytest.raises(RequestError) as failure:
            await agent.prompt(session, [block("/compact")])
        assert failure.value.data == {"reason": "uncertain compaction"}
        assert str(failure.value) == "uncertain compaction"
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_attached_compact_routes_to_owner_not_attached_model(tmp_path):
    agent = canonical_agent(wire(tmp_path / "wire"), auto_wake=False)
    calls = []

    class Proxy:
        async def request(self, action, **kwargs):
            calls.append((action, kwargs))
            return {"ok": True, "status": "compacted"}

    agent.sessions.proxies["attached"] = Proxy()
    response = await agent.prompt("attached", [block(" ")], agentComms={"compact": None})
    assert response.stop_reason == "end_turn"
    assert response.field_meta == {
        "agentComms": {"compaction": {"ok": True, "status": "compacted"}}
    }
    assert calls == [("compact", {"instructions": ""})]


@pytest.mark.asyncio
async def test_compact_rejects_multimodal_and_overlong_without_bridge_or_model(tmp_path):
    agent = canonical_agent(wire(tmp_path / "wire"), auto_wake=False)
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


async def test_current_root_compact_refuses_unjournaled_writer_for_every_launch_form(
    tmp_path
):
    """Actual ACP request uses root authority, never executable-name inference."""
    comms = wire(tmp_path / "wire")
    root_id = comms.messaging.initialize_private_initial_protocol()
    package = tmp_path / "native-package"
    agent = CommsAgent(
        comms,
        auto_wake=False,
        private_nk_native_package=package,
        private_nk_wire_root_id=root_id,
    )
    session = (await agent.new_session(cwd=str(tmp_path / "work"))).session_id
    saved = tmp_path / "native.jsonl"
    saved.write_text('{"type":"session","version":3,"id":"preserved"}\n')
    comms.threads.attach_session(session, str(saved), pid=os.getpid())
    before = saved.read_bytes()
    alias = tmp_path / "renamed-native"
    alias.symlink_to(package / "dist/cli.js")

    try:
        for command in ("pi", str(package / "dist/cli.js"), str(alias)):
            agent.turns.agent_bin = command
            with pytest.raises(RequestError, match="requires the current selected native session"):
                await build_agent_router(agent)(
                    "session/prompt",
                    {"sessionId": session, "prompt": [{"type": "text", "text": "/compact"}]},
                    False,
                )
            assert saved.read_bytes() == before
            assert comms.registry.require(session).active_turn is None
    finally:
        await agent.shutdown()
