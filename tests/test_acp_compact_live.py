"""No-credential actual ACP router -> owner bridge -> installed Pi saved-session proof."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from acp import RequestError
from acp.agent.router import build_agent_router

from agent_comms.acp import CommsAgent
from agent_comms.operations import wire
from test_manual_compaction import LoopbackProvider, saved_session, wrapper

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")


@pytest.mark.parametrize("status", [200, 503])
async def test_toad_wire_compact_uses_loopback_and_saved_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    provider = LoopbackProvider(status=status)
    server = await asyncio.start_server(provider.handle, "127.0.0.1", 0)
    agent = None
    try:
        worktree = tmp_path / "work"
        worktree.mkdir()
        session_file = worktree / "existing.jsonl"
        before = saved_session(session_file)
        exe = wrapper(tmp_path, monkeypatch, server.sockets[0].getsockname()[1])
        agent = CommsAgent(
            wire(tmp_path / "wire"),
            agent_bin=exe,
            agent_args=["--provider", "openrouter", "--model", "fake-compact"],
            auto_wake=False,
        )
        session = (await agent.new_session(cwd=str(worktree))).session_id
        agent._comms.attach_session(session, str(session_file), pid=os.getpid())
        updates = []

        class Client:
            async def session_update(self, *, session_id, update):
                updates.append(update)

        agent.on_connect(Client())
        call = build_agent_router(agent)(
            "session/prompt",
            {
                "sessionId": session,
                "prompt": [{"type": "text", "text": " "}],
                "_meta": {"agentComms": {"compact": None}},
            },
            False,
        )
        if status == 200:
            response = await call
            assert response.stop_reason == "end_turn"
            assert response.field_meta["agentComms"]["compaction"]["ok"] is True
            assert session_file.read_bytes().startswith(before)
            assert json.loads(session_file.read_bytes().splitlines()[-1])["type"] == "compaction"
        else:
            with pytest.raises(RequestError):
                await call
            assert session_file.read_bytes().startswith(before)
            assert not any(
                json.loads(row)["type"] == "compaction"
                for row in session_file.read_bytes().splitlines()
            )
        assert provider.posts >= 1
        phases = [
            item.field_meta["agentComms"]["compaction"]["phase"]
            for item in updates
            if (getattr(item, "field_meta", None) or {}).get("agentComms", {}).get("compaction")
        ]
        assert phases == ["start", "end" if status == 200 else "abort"]
    finally:
        if agent is not None:
            await agent.shutdown()
        server.close()
        await server.wait_closed()
