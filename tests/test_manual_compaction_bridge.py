"""Idle-owner bridge tests; the published runtime lacks the /compact router."""

import asyncio
import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.manual_compaction_bridge import compact_context
from agent_comms.operations import wire
from test_manual_compaction import saved_session


async def _owner(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    owner = CommsAgent(wire(tmp_path / "wire"), agent_bin="pi", agent_args=[])
    updates = []

    class Client:
        async def session_update(self, *, update, **_kwargs):
            updates.append(update)

    owner._client = Client()
    await owner.new_session(cwd=str(project), mcp_servers=[])
    session = tmp_path / "saved.jsonl"
    saved_session(session)
    owner._comms.attach_session("project", str(session), pid=os.getpid())
    owner._comms.set_agent_info("project", context_used=880, context_size=1000)
    return owner, updates


def _metadata(updates):
    return [
        update.field_meta["agentComms"]
        for update in updates
        if (getattr(update, "field_meta", None) or {}).get("agentComms")
    ]


async def test_bridge_success_unknown_usage_and_one_explicit_request(tmp_path, monkeypatch):
    owner, updates = await _owner(tmp_path)
    requests = []

    async def one(*args, **kwargs):
        requests.append((args, kwargs))
        return {"ok": True, "summary": "local summary", "estimatedTokensAfter": 100}

    monkeypatch.setattr("agent_comms.manual_compaction.compact_session", one)
    try:
        result = await compact_context(owner, "project", "Focus on facts")
        assert result["ok"] is True
        assert len(requests) == 1
        assert requests[0][0][-1] == "Focus on facts"
        metadata = _metadata(updates)
        phases = [entry["compaction"] for entry in metadata if "compaction" in entry]
        assert [phase["phase"] for phase in phases] == ["start", "end"]
        assert all(
            phase["contextState"] == "unknown"
            and phase["contextUsed"] is None
            and phase["willRetry"] is False
            for phase in phases
        )
        assert any(entry.get("transcriptChanged") is True for entry in metadata)
        assert owner._comms.agent_info_of("project").context_used is None
        assert owner._comms.registry.require("project").active_turn is None
    finally:
        await owner.shutdown()


async def test_bridge_busy_then_cancel_never_replays(tmp_path, monkeypatch):
    owner, updates = await _owner(tmp_path)
    entered = asyncio.Event()
    calls = []

    async def uncertain(*args, **kwargs):
        calls.append((args, kwargs))
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("agent_comms.manual_compaction.compact_session", uncertain)
    task = asyncio.create_task(compact_context(owner, "project"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        busy = await compact_context(owner, "project")
        assert busy["ok"] is False
        assert len(calls) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        phases = [item["compaction"] for item in _metadata(updates) if "compaction" in item]
        assert [phase["phase"] for phase in phases] == ["start", "abort"]
        assert not any(entry.get("transcriptChanged") is True for entry in _metadata(updates))
        assert owner._comms.agent_info_of("project").context_used is None
        assert owner._comms.registry.require("project").active_turn is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await owner.shutdown()
