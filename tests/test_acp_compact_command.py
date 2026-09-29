"""No-provider ACP routing tests for explicit saved-session /compact.

These tests use a fake bridge and assert the command never reaches an agent turn.
The runner's separate fake-provider tests establish no-retry and session safety.
"""

from __future__ import annotations

import pytest
from acp import RequestError
from acp.agent.router import build_agent_router
from acp.schema import TextContentBlock

from agent_comms.acp_extension import CompactRequest, encode_request
from agent_comms.comms import wire
from agent_comms.compaction_result import CommittedCompactionResult, RefusedCompactionResult
from agent_comms.field_codec import FieldCodec
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
        return CommittedCompactionResult("summary", "commit")

    async def forbidden_model(*args, **kwargs):
        raise AssertionError("/compact must not become a model prompt")

    monkeypatch.setattr("agent_comms.manual_compaction_bridge.compact_context", compact_context)
    monkeypatch.setattr(agent.turns, "run_agent_turn", forbidden_model)
    try:
        response = await agent.prompt(session, [block("/compact   focus on safety ")])
        assert response.stop_reason == "end_turn"
        assert response == CommittedCompactionResult("summary", "commit").prompt_response()
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
        return CommittedCompactionResult("summary", "commit")

    async def forbidden_model(*args, **kwargs):
        raise AssertionError("Toad's compact metadata must not be a model prompt")

    monkeypatch.setattr("agent_comms.manual_compaction_bridge.compact_context", compact_context)
    monkeypatch.setattr(agent.turns, "run_agent_turn", forbidden_model)
    try:
        response = await agent.prompt(
            session, [block(" ")], _meta=encode_request(CompactRequest("focus on safety"))
        )
        assert response.stop_reason == "end_turn"
        assert response == CommittedCompactionResult("summary", "commit").prompt_response()
        assert calls == [(agent.turns, session, "focus on safety")]
        with pytest.raises(RequestError) as invalid:
            await agent.prompt(session, [block("nonblank")], _meta=encode_request(CompactRequest()))
        assert invalid.value.data == {"reason": "Compaction metadata requires blank text"}
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_sdk_router_preserves_toad_wire_metadata(tmp_path, monkeypatch):
    agent = canonical_agent(wire(tmp_path / "wire"), auto_wake=False)
    session = (await agent.new_session(cwd=str(tmp_path / "work"))).session_id
    calls = []

    async def compact_context(owner, session_id, instructions):
        calls.append((owner, session_id, instructions))
        return CommittedCompactionResult("summary", "commit")

    monkeypatch.setattr("agent_comms.manual_compaction_bridge.compact_context", compact_context)
    try:
        response = await build_agent_router(agent)(
            "session/prompt",
            {
                "sessionId": session,
                "prompt": [{"type": "text", "text": " "}],
                "_meta": encode_request(CompactRequest("focus")),
            },
            False,
        )
        assert response == CommittedCompactionResult("summary", "commit").prompt_response()
        assert calls == [(agent.turns, session, "focus")]
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_compact_failure_is_not_end_turn_success(tmp_path, monkeypatch):
    agent = canonical_agent(wire(tmp_path / "wire"), auto_wake=False)
    session = (await agent.new_session(cwd=str(tmp_path / "work"))).session_id

    async def compact_context(*args):
        return RefusedCompactionResult("uncertain compaction")

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
            return FieldCodec.encode(CommittedCompactionResult("summary", "commit"))

    agent.sessions.proxies["attached"] = Proxy()
    response = await agent.prompt("attached", [block(" ")], _meta=encode_request(CompactRequest()))
    assert response.stop_reason == "end_turn"
    assert response == CommittedCompactionResult("summary", "commit").prompt_response()
    assert calls == [("compact", {"instructions": None})]


@pytest.mark.asyncio
async def test_compact_rejects_multimodal_and_overlong_without_bridge_or_model(tmp_path):
    agent = canonical_agent(wire(tmp_path / "wire"), auto_wake=False)
    session = (await agent.new_session(cwd=str(tmp_path / "work"))).session_id
    try:
        with pytest.raises(RequestError) as multimodal:
            await agent.prompt(session, [block("/compact"), {"type": "image", "data": "abc"}])
        assert multimodal.value.data == {"reason": "/compact requires one text block."}
        with pytest.raises(ValueError) as oversized:
            await agent.prompt(session, [block("/compact " + "x" * 2001)])
        assert str(oversized.value) == "Compaction instructions are too long"
    finally:
        await agent.shutdown()
