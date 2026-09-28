"""Idle-owner bridge tests; the published runtime lacks the /compact router."""

import asyncio
import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.comms import wire
from agent_comms.manual_compaction_bridge import compact_context
from test_manual_compaction import saved_session


async def _owner(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    owner = CommsAgent(wire(tmp_path / "wire"), agent_bin="pi", agent_args=[])
    updates = []

    class Client:
        async def session_update(self, *, update, **_kwargs):
            updates.append(update)

    owner.sessions.client = Client()
    await owner.new_session(cwd=str(project), mcp_servers=[])
    session = tmp_path / "saved.jsonl"
    saved_session(session)
    owner._comms.threads.attach_session("project", str(session), pid=os.getpid())
    owner._comms.agents.set_agent_info("project", context_used=880, context_size=1000)
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

    monkeypatch.setattr("agent_comms.manual_compaction.ManualCompaction.run", one)
    try:
        result = await compact_context(owner.turns, "project", "Focus on facts")
        assert result["ok"] is True
        assert len(requests) == 1
        assert requests[0][0][0].instructions == "Focus on facts"
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
        assert owner._comms.agents.agent_info_of("project").context_used is None
        assert owner._comms.registry.require("project").active_turn is None
    finally:
        await owner.shutdown()


async def test_bridge_closes_idle_pi_before_saved_session_writer(tmp_path, monkeypatch):
    owner, _updates = await _owner(tmp_path)

    class Retained:
        closed = False

        async def close_idle(self):
            self.closed = True

    retained = Retained()
    owner.turns.persistent_backends["project"] = retained

    async def compact(*_args, **_kwargs):
        assert retained.closed
        return {"ok": True, "summary": "local summary"}

    monkeypatch.setattr("agent_comms.manual_compaction.ManualCompaction.run", compact)
    try:
        assert (await compact_context(owner.turns, "project"))["ok"] is True
    finally:
        await owner.shutdown()


async def test_bridge_uses_persisted_model_when_worker_has_no_base_args(tmp_path, monkeypatch):
    owner, _updates = await _owner(tmp_path)
    owner._comms.threads.set_thread_model("project", "openai-codex/gpt-5.5")
    calls = []

    async def compact(*args, **_kwargs):
        calls.append(args)
        return {"ok": True, "summary": "local summary"}

    monkeypatch.setattr("agent_comms.manual_compaction.ManualCompaction.run", compact)
    try:
        assert (await compact_context(owner.turns, "project"))["ok"] is True
        assert calls[0][0].agent_args == ("--provider", "openai-codex", "--model", "gpt-5.5")
    finally:
        await owner.shutdown()


async def test_bridge_abort_exposes_safe_failure_reason(tmp_path, monkeypatch):
    owner, updates = await _owner(tmp_path)

    async def failed(*_args, **_kwargs):
        return {"ok": False, "error": "Compaction provider returned HTTP 400."}

    monkeypatch.setattr("agent_comms.manual_compaction.ManualCompaction.run", failed)
    try:
        result = await compact_context(owner.turns, "project")
        assert result == {"ok": False, "error": "Compaction provider returned HTTP 400."}
        phases = [item["compaction"] for item in _metadata(updates) if "compaction" in item]
        assert [phase["phase"] for phase in phases] == ["start", "abort"]
        assert phases[-1]["summary"] == result["error"]
        assert phases[-1]["contextState"] == "unknown"
        assert phases[-1]["willRetry"] is False
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

    monkeypatch.setattr("agent_comms.manual_compaction.ManualCompaction.run", uncertain)
    task = asyncio.create_task(compact_context(owner.turns, "project"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        busy = await compact_context(owner.turns, "project")
        assert busy["ok"] is False
        assert len(calls) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        phases = [item["compaction"] for item in _metadata(updates) if "compaction" in item]
        assert [phase["phase"] for phase in phases] == ["start", "abort"]
        assert not any(entry.get("transcriptChanged") is True for entry in _metadata(updates))
        assert owner._comms.agents.agent_info_of("project").context_used is None
        assert owner._comms.registry.require("project").active_turn is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await owner.shutdown()


async def test_bridge_releases_waiters_after_settlement_publication(tmp_path, monkeypatch):
    owner, updates = await _owner(tmp_path)
    released = []
    release = owner._comms.goals.release_waits_after_terminal_turn

    async def compact(*_args, **_kwargs):
        return {"ok": True, "summary": "local summary"}

    def observed_release(fence):
        assert fence is not None
        assert any(
            item.get("turnSettled") is True and item.get("turnId") == fence.turn_id
            for item in _metadata(updates)
        )
        released.append(fence)
        release(fence)

    monkeypatch.setattr("agent_comms.manual_compaction.ManualCompaction.run", compact)
    monkeypatch.setattr(owner._comms.goals, "release_waits_after_terminal_turn", observed_release)
    try:
        assert (await compact_context(owner.turns, "project"))["ok"] is True
        assert len(released) == 1
    finally:
        await owner.shutdown()
