"""Goal edits/history use the actual owner socket and persisted backend state."""

import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.operations import wire
from agent_comms.runtime import RuntimeProxy, socket_path


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")
async def test_goal_edit_and_history_over_owner_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    # Keep this non-provider test's goal paused. Edits must preserve that state.
    response = await owner.new_session(str(tmp_path / "project"))
    session = response.session_id
    goal = comms.update_goal(session, "set", text="Review @child's implementation")
    paused = comms.update_goal(session, "paused", owner_action=True)
    proxy = RuntimeProxy(CommsAgent(comms), session, socket_path(comms.root, os.getpid()))
    try:
        result = await proxy.request(
            "edit_goal",
            goal_id=goal.id,
            expected_revision=paused.revision,
            text="Review @child's implementation and record the evidence",
        )
        changed = wire(comms.root).registry.require(session).goal
        assert result["goal"]["id"] == goal.id == changed.id
        assert changed.status == "paused"
        assert changed.text == result["goal"]["text"]
        assert changed.revision == paused.revision + 1
        assert changed.progress == paused.progress
        assert comms.goal_pause(session).source == "owner"
        assert result["goalExecution"]["state"] == "paused"
        history = (await proxy.request("goal_history", goal_id=goal.id))["history"]
        assert history[-1]["before"]["text"] == paused.text
        assert history[-1]["after"] == result["goal"]
        with pytest.raises(RuntimeError, match="changed"):
            await proxy.request(
                "edit_goal", goal_id=goal.id, expected_revision=paused.revision, text="stale edit"
            )
        assert wire(comms.root).registry.require(session).goal == changed
        assert (await proxy.request("goal_history", goal_id=goal.id))["history"] == history
    finally:
        await proxy.close()
        await owner.shutdown()
