"""No-provider ACP routing tests for explicit saved-session /compact.

These tests use a fake bridge and assert the command never reaches an agent turn.
The runner's separate fake-provider tests establish no-retry and session safety.
"""

from __future__ import annotations

import pytest
from acp import RequestError
from acp.schema import ImageContentBlock, PromptResponse, TextContentBlock

from agent_comms.acp_extension import (
    CompactionCommittedUpdate,
    CompactRequest,
    encode_request,
    encode_updates,
)
from agent_comms.agent_backend import PlacedCompaction
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.pi_counts import PiCount
from delivery_owner_fixture import canonical_agent

PLACED = PlacedCompaction("summary", "first-kept", PiCount.from_wire(1200))
PLACED_REPLY = PromptResponse(
    stop_reason="end_turn",
    field_meta=encode_updates(CompactionCommittedUpdate("summary", "first-kept")),
)


def block(text: str) -> TextContentBlock:
    return TextContentBlock(type="text", text=text)


@pytest.mark.asyncio
async def test_attached_compact_routes_to_owner_not_attached_model(tmp_path, native_runtime_package):
    agent = canonical_agent(wire(tmp_path / "wire"), auto_wake=False)
    calls = []

    class Proxy:
        async def request(self, action, **kwargs):
            calls.append((action, kwargs))
            return FieldCodec.encode(PLACED)

    agent.sessions.proxies["attached"] = Proxy()
    response = await agent.prompt("attached", [block(" ")], _meta=encode_request(CompactRequest()))
    assert response.stop_reason == "end_turn"
    assert response == PLACED_REPLY
    assert calls == [("compact", {"instructions": None})]


@pytest.mark.asyncio
async def test_compact_rejects_multimodal_and_overlong_without_bridge_or_model(
    tmp_path, native_runtime_package
):
    agent = canonical_agent(wire(tmp_path / "wire"), auto_wake=False)
    session = (await agent.new_session(cwd=str(tmp_path / "work"))).session_id
    try:
        with pytest.raises(RequestError) as multimodal:
            await agent.prompt(
                session,
                [
                    block("/compact"),
                    ImageContentBlock(type="image", data="abc", mime_type="image/png"),
                ],
            )
        assert multimodal.value.data == {"reason": "/compact requires one text block."}
        with pytest.raises(ValueError) as oversized:
            await agent.prompt(session, [block("/compact " + "x" * 2001)])
        assert str(oversized.value) == "Compaction instructions are too long"
    finally:
        await agent.shutdown()
