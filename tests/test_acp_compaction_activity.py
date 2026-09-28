"""Automatic compaction must reach the activity authority used by every sidebar."""

import pytest

from agent_comms import agent_events as ae
from agent_comms.acp import CommsAgent
from agent_comms.comms import wire
from agent_comms.declarations import ActivityState


@pytest.mark.asyncio
@pytest.mark.parametrize("aborted", [False, True])
async def test_compaction_activity_survives_tool_updates_and_restores_latest_state(
    tmp_path, monkeypatch, aborted
):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", auto_wake=False)
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _session: None)
    await agent.new_session(str(tmp_path / "project"))

    def activity(state, detail):
        current = comms.agents.activity_of("project")
        assert (current.state, current.detail) == (state, detail)
        view = next(item for item in comms.views.thread_views() if item.thread.name == "project")
        assert view.activity == current
        assert detail in view.presentation.summary

    async def events(*args, **kwargs):
        yield ae.ToolStart(id="old-tool", name="read", title="Read project")
        activity(ActivityState.WORKING, "Read project")
        yield ae.CompactionStart(reason="threshold")
        activity(ActivityState.WORKING, "Compacting context")
        yield ae.CompactionProgress(chunk_index=2)
        activity(ActivityState.WORKING, "Compacting context")
        yield ae.ToolEnd(id="old-tool", name="read", ok=True)
        activity(ActivityState.WORKING, "Compacting context")
        yield ae.ToolStart(id="next-tool", name="read", title="Read next file")
        activity(ActivityState.WORKING, "Compacting context")
        yield ae.CompactionEnd(aborted=aborted, summary="saved summary")
        activity(ActivityState.WORKING, "Read next file")
        yield ae.ToolEnd(id="next-tool", name="read", ok=True)
        assert comms.agents.activity_of("project").state is ActivityState.THINKING
        yield ae.StreamSettled()
        assert comms.agents.activity_of("project").state is ActivityState.IDLE
        yield ae.Done(ok=not aborted, text="Done")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        await agent.turns.run_agent_turn("project", "project", "Read these files")
        assert comms.agents.activity_of("project").state is ActivityState.IDLE
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_compaction_eof_still_finishes_activity(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", auto_wake=False)
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _session: None)
    await agent.new_session(str(tmp_path / "project"))

    async def events(*args, **kwargs):
        yield ae.CompactionStart(reason="threshold")
        assert comms.agents.activity_of("project").detail == "Compacting context"

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        await agent.turns.run_agent_turn("project", "project", "Read files")
        assert comms.agents.activity_of("project").state is ActivityState.IDLE
    finally:
        await agent.shutdown()
