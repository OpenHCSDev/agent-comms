"""Goal edits/history use the actual owner socket and persisted backend state."""
from agent_comms.controller import SessionUpdateSink

import asyncio
import os

import pytest

from delivery_owner_fixture import canonical_agent
from agent_comms.acp_extension import GoalChangedUpdate, decode_updates
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_presentation import GoalExecution
from agent_comms.goal_actions import (
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.native_arguments import GivenArguments, GivenBinary


def goal_updates(metadata) -> tuple[GoalChangedUpdate, ...]:
    """The goal snapshots one Comms update batch carries."""
    return tuple(update for update in decode_updates(metadata) if isinstance(update, GoalChangedUpdate))


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")
async def test_goal_edit_and_history_over_owner_socket(tmp_path, monkeypatch, native_runtime_package):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    owner = canonical_agent(comms, agent_bin=GivenBinary("pi"), agent_args=GivenArguments(()), runtime_enabled=True)
    # Keep this non-provider test's goal paused. Edits must preserve that state.
    response = await owner.new_session(str(tmp_path / "project"))
    session = response.session_id
    goal = comms.goals.update_goal(session, SetGoalAction(text="Review @child's implementation"))
    paused = comms.goals.update_goal(session, PausedGoalAction(), actor=OwnerInvocable)
    updates = []

    class Client(SessionUpdateSink):
        async def publish_update(self, session_id, update):
            updates.append(update.model_dump(mode="json", by_alias=True, exclude_none=True))
            return True

    observer = canonical_agent(comms)
    observer.on_connect(Client())
    proxy = RuntimeProxy(observer, session, socket_path(comms.root, os.getpid()))
    try:
        (presented,) = goal_updates(await proxy.subscribe())
        assert presented.goal == paused
        assert presented.execution.declared_name == "paused"
        result = await proxy.request(
            "edit_goal",
            goal_id=goal.id,
            expected_revision=paused.revision,
            text="Review @child's implementation and record the evidence",
        )
        changed = wire(comms.root).registry.require(session).goal
        assert result["goal"]["id"] == goal.id == changed.id
        assert changed.state.declared_name == "paused"
        assert changed.text == result["goal"]["text"]
        assert changed.revision == paused.revision + 1
        assert changed.progress == paused.progress
        assert comms.registry.require(session).goal.state.pause_source.declared_name == "owner"
        assert FieldCodec.decode(GoalExecution, result["goalExecution"]).declared_name == "paused"
        async with asyncio.timeout(2):
            while changed not in (
                published.goal for update in updates for published in goal_updates(update.get("_meta"))
            ):
                await asyncio.sleep(0.01)
        history = (await proxy.request("goal_history", goal_id=goal.id))["history"]
        assert history[-1]["before"]["text"] == paused.text
        assert history[-1]["after"] == result["goal"]
        with pytest.raises(RuntimeError):
            await proxy.request(
                "edit_goal", goal_id=goal.id, expected_revision=paused.revision, text="stale edit"
            )
        assert wire(comms.root).registry.require(session).goal == changed
        assert (await proxy.request("goal_history", goal_id=goal.id))["history"] == history
    finally:
        await proxy.close()
        await owner.shutdown()
