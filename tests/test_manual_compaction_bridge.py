"""Real local runtime→ACP-owner→one-shot Pi RPC compact integration, no provider."""

import asyncio
import json
import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.manual_compaction_bridge import compact_context
from agent_comms.operations import wire
from test_manual_compaction import _session, _stub

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unix socket and POSIX executable stub")


@pytest.mark.parametrize("mode", ["success", "failure"])
async def test_idle_compact_runtime_reaches_real_acp_owner_and_resets_stale_usage(
    tmp_path, monkeypatch, mode
):
    stub, pid_file, capture = _stub(tmp_path, monkeypatch, mode)
    project = tmp_path / "proj"
    project.mkdir()
    agent = CommsAgent(wire(tmp_path / "wire"), agent_bin=stub, agent_args=[])
    updates = []

    class FakeClient:
        async def session_update(self, *, update, **kwargs):
            updates.append(update)

    agent._client = FakeClient()
    await agent.new_session(cwd=str(project), mcp_servers=[])
    agent._comms.attach_session("proj", _session(tmp_path), pid=os.getpid())
    agent._comms.set_agent_info("proj", context_used=880, context_size=1000)
    await agent._runtime.start()
    reader, writer = await asyncio.open_unix_connection(str(agent._runtime.path))
    try:
        command = {"thread": "proj", "action": "compact", "instructions": "Keep facts"}
        writer.write((json.dumps(command) + "\n").encode())
        await writer.drain()
        async with asyncio.timeout(5):
            while True:
                response = json.loads(await reader.readline())
                if "result" in response or "error" in response:
                    break
        assert "error" not in response, response
        assert response["result"]["ok"] is (mode == "success")
        assert json.loads(capture.read_text())["customInstructions"] == "Keep facts"
        assert pid_file.exists()
        meta = [
            update.field_meta["agentComms"]
            for update in updates
            if (getattr(update, "field_meta", None) or {}).get("agentComms")
        ]
        compaction = [entry["compaction"] for entry in meta if "compaction" in entry]
        assert [entry["phase"] for entry in compaction] == [
            "start",
            "end" if mode == "success" else "abort",
        ]
        assert all(entry["contextState"] == "unknown" for entry in compaction)
        assert all(entry["contextUsed"] is None for entry in compaction)
        assert not [update for update in updates if update.session_update == "usage_update"]
        assert any("transcriptChanged" in entry for entry in meta) is (mode == "success")
        assert agent._comms.agent_info_of("proj").context_used is None
        assert agent._comms.registry.require("proj").active_turn is None
        assert not agent._active_turns
    finally:
        writer.close()
        await writer.wait_closed()
        await agent.shutdown()


@pytest.mark.parametrize("mode", ["success", "failure"])
async def test_terminal_compaction_update_delivered_then_client_raises_is_not_duplicated(
    tmp_path, monkeypatch, mode
):
    stub, pid_file, _ = _stub(tmp_path, monkeypatch, mode)
    project = tmp_path / "proj"
    project.mkdir()
    agent = CommsAgent(wire(tmp_path / "wire"), agent_bin=stub, agent_args=[])
    updates = []

    class DisconnectAfterTerminal:
        async def session_update(self, *, update, **kwargs):
            updates.append(update)
            meta = (getattr(update, "field_meta", None) or {}).get("agentComms", {})
            if meta.get("compaction", {}).get("phase") in {"end", "abort"}:
                raise ConnectionError("client recorded terminal update then disconnected")

    agent._client = DisconnectAfterTerminal()
    await agent.new_session(cwd=str(project), mcp_servers=[])
    agent._comms.attach_session("proj", _session(tmp_path), pid=os.getpid())
    agent._comms.set_agent_info("proj", context_used=880, context_size=1000)
    try:
        with pytest.raises(ConnectionError, match="recorded terminal update"):
            await compact_context(agent, "proj")
        phases = [
            update.field_meta["agentComms"]["compaction"]["phase"]
            for update in updates
            if (getattr(update, "field_meta", None) or {}).get("agentComms", {}).get("compaction")
        ]
        assert phases == ["start", "end" if mode == "success" else "abort"]
        assert agent._comms.agent_info_of("proj").context_used is None
        assert agent._comms.registry.require("proj").active_turn is None
        assert not agent._active_turns
        assert pid_file.exists()
    finally:
        await agent.shutdown()


async def test_idle_compact_rejects_no_saved_session_without_launch(tmp_path, monkeypatch):
    stub, pid_file, _ = _stub(tmp_path, monkeypatch, "success")
    project = tmp_path / "proj"
    project.mkdir()
    agent = CommsAgent(wire(tmp_path / "wire"), agent_bin=stub, agent_args=[])
    await agent.new_session(cwd=str(project), mcp_servers=[])
    assert await compact_context(agent, "proj") == {
        "ok": False,
        "error": "This thread has no persisted context to compact.",
    }
    assert not pid_file.exists()
    await agent.shutdown()


async def test_idle_compact_busy_request_does_not_launch_twice_and_cancel_aborts(
    tmp_path, monkeypatch
):
    stub, pid_file, capture = _stub(tmp_path, monkeypatch, "hang")
    project = tmp_path / "proj"
    project.mkdir()
    agent = CommsAgent(wire(tmp_path / "wire"), agent_bin=stub, agent_args=[])
    updates = []

    class FakeClient:
        async def session_update(self, *, update, **kwargs):
            updates.append(update)

    agent._client = FakeClient()
    await agent.new_session(cwd=str(project), mcp_servers=[])
    agent._comms.attach_session("proj", _session(tmp_path), pid=os.getpid())
    task = asyncio.create_task(compact_context(agent, "proj"))
    try:
        async with asyncio.timeout(3):
            while not capture.exists():
                await asyncio.sleep(0.01)
        assert await compact_context(agent, "proj") == {
            "ok": False,
            "error": "Wait for the current response before compacting.",
        }
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=4)
        meta = [
            update.field_meta["agentComms"]
            for update in updates
            if (getattr(update, "field_meta", None) or {}).get("agentComms")
        ]
        assert [entry["compaction"]["phase"] for entry in meta if "compaction" in entry] == [
            "start",
            "abort",
        ]
        assert agent._comms.registry.require("proj").active_turn is None
        assert not agent._active_turns
        assert pid_file.exists()
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await agent.shutdown()
