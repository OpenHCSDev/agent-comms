"""A UI goal must acquire owner-private launch authority before it is visible."""

import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import BlockedGoalAction, GoalPrecondition, SetGoalAction
from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.goal_generation import ReadyGeneration
from agent_comms.goals import Goal
from agent_comms.runtime import RuntimeProxy, socket_path

pytestmark = pytest.mark.skipif(os.name == "nt", reason="ACP runtime uses Unix domain sockets")


@pytest.fixture(autouse=True)
def _models_without_pi_discovery(monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "openrouter/fake")


async def _owner(tmp_path, monkeypatch):
    comms = wire(tmp_path / "wire")
    private = comms.root / "goal-private"
    private.mkdir(mode=0o700)
    GoalAttemptStore.initialize(private)
    owner = CommsAgent(
        comms,
        agent_bin="pi",
        agent_args=["--provider", "openrouter", "--model", "fake"],
        runtime_enabled=True,
        auto_wake=False,
    )
    session = (await owner.new_session(cwd=str(tmp_path / "project"))).session_id
    wakes = []
    monkeypatch.setattr(owner.turns, "schedule_goal", wakes.append)
    proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
    return comms, owner, proxy, session, wakes


async def test_ui_set_goal_creates_ledger_before_reporting_success(tmp_path, monkeypatch):
    comms, owner, proxy, session, wakes = await _owner(tmp_path, monkeypatch)
    try:
        result = await proxy.request("set_goal", text="testing goal")
        goal = result["goal"]
        assert FieldCodec.decode(Goal, goal).state.declared_name == "active"
        assert comms.registry.require(session).goal.id == goal["id"]
        generation = GoalAttemptStore(comms.root / "goal-private").snapshot(goal["id"])
        assert generation is not None and generation.lifecycle == ReadyGeneration()
        assert owner.turns.goal_store.ready_grant(goal["id"], generation.number)
        assert session in wakes
    finally:
        await owner.shutdown()


async def test_explicit_retry_cannot_invent_missing_launch_authority(tmp_path, monkeypatch):
    comms, owner, proxy, session, wakes = await _owner(tmp_path, monkeypatch)
    try:
        goal = comms.goals.update_goal(session, SetGoalAction(text="Goal without authority"))
        blocked = comms.goals.update_goal(
            session,
            BlockedGoalAction(
                expect=GoalPrecondition(goal_id=goal.id), progress="Goal attempt unresolved"
            ),
        )
        store = GoalAttemptStore(comms.root / "goal-private")
        assert store.snapshot(goal.id) is None
        with pytest.raises(RuntimeError, match="Retry cannot create a grant"):
            await proxy.request("retry_goal", goal_id=goal.id, expected_revision=blocked.revision)
        assert store.snapshot(goal.id) is None
        assert comms.registry.require(session).goal == blocked
        assert wakes == []
    finally:
        await owner.shutdown()
