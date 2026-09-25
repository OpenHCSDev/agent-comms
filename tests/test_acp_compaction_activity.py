"""Automatic compaction must reach the activity authority used by every sidebar."""

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.declarations import ActivityState
from agent_comms.operations import wire


@pytest.mark.asyncio
@pytest.mark.parametrize("aborted", [False, True])
async def test_compaction_activity_survives_tool_updates_and_restores_latest_state(
    tmp_path, monkeypatch, aborted
):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", auto_wake=False)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    await agent.new_session(str(tmp_path / "project"))

    def activity(state, detail):
        current = comms.activity_of("project")
        assert (current.state, current.detail) == (state, detail)
        view = next(item for item in comms.thread_views() if item.thread.name == "project")
        assert view.activity == current
        assert detail in view.presentation.summary

    async def events(*args, **kwargs):
        yield {"type": "tool_start", "id": "old-tool", "name": "read", "title": "Read project"}
        activity(ActivityState.WORKING, "Read project")
        yield {"type": "compaction_start", "reason": "threshold"}
        activity(ActivityState.WORKING, "Compacting context")
        yield {"type": "compaction_progress", "chunk_index": 2}
        activity(ActivityState.WORKING, "Compacting context")
        yield {"type": "tool_end", "id": "old-tool", "name": "read", "ok": True}
        activity(ActivityState.WORKING, "Compacting context")
        yield {"type": "tool_start", "id": "next-tool", "name": "read", "title": "Read next file"}
        activity(ActivityState.WORKING, "Compacting context")
        yield {"type": "compaction_end", "aborted": aborted, "summary": "saved summary"}
        activity(ActivityState.WORKING, "Read next file")
        yield {"type": "tool_end", "id": "next-tool", "name": "read", "ok": True}
        assert comms.activity_of("project").state is ActivityState.THINKING
        yield {"type": "settled"}
        assert comms.activity_of("project").state is ActivityState.IDLE
        yield {"type": "done", "ok": not aborted, "text": "Done"}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent._run_agent_turn("project", "project", "Read these files")
        assert comms.activity_of("project").state is ActivityState.IDLE
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_compaction_eof_still_finishes_activity(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", auto_wake=False)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    await agent.new_session(str(tmp_path / "project"))

    async def events(*args, **kwargs):
        yield {"type": "compaction_start", "reason": "threshold"}
        assert comms.activity_of("project").detail == "Compacting context"

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent._run_agent_turn("project", "project", "Read files")
        assert comms.activity_of("project").state is ActivityState.IDLE
    finally:
        await agent.shutdown()
