"""A UI goal must acquire owner-private launch authority before it is visible."""

import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.operations import wire
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
    monkeypatch.setattr(owner, "_schedule_goal", wakes.append)
    proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
    return comms, owner, proxy, session, wakes


async def test_ui_set_goal_creates_ledger_before_reporting_success(tmp_path, monkeypatch):
    comms, owner, proxy, session, wakes = await _owner(tmp_path, monkeypatch)
    try:
        result = await proxy.request("set_goal", text="testing goal")
        goal = result["goal"]
        assert goal["status"] == "active"
        assert comms.registry.require(session).goal.id == goal["id"]
        generation = GoalAttemptStore(comms.root / "goal-private").snapshot(goal["id"])
        assert generation is not None and generation.state == "ready"
        assert owner._goal_store.ready_grant(goal["id"], generation.number)
        assert session in wakes
    finally:
        await owner.shutdown()


async def test_explicit_retry_recovers_registry_goal_missing_ledger(tmp_path, monkeypatch):
    comms, owner, proxy, session, wakes = await _owner(tmp_path, monkeypatch)
    try:
        legacy = comms.update_goal(session, "set", text="older UI goal")
        assert legacy is not None
        blocked = comms.update_goal(
            session, "blocked", goal_id=legacy.id, progress="Goal attempt unresolved"
        )
        assert blocked is not None
        assert GoalAttemptStore(comms.root / "goal-private").snapshot(legacy.id) is None
        result = await proxy.request(
            "retry_goal", goal_id=legacy.id, expected_revision=blocked.revision
        )
        assert result["goal"]["id"] == legacy.id
        assert result["goal"]["status"] == "active"
        generation = GoalAttemptStore(comms.root / "goal-private").snapshot(legacy.id)
        assert generation is not None and generation.state == "ready" and generation.number == 2
        assert owner._goal_store.ready_grant(legacy.id, generation.number)
        assert session in wakes
    finally:
        await owner.shutdown()
