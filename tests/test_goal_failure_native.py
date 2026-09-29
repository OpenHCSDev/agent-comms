"""Saved native history -> ACP goal failure -> passive read, without replay."""

import asyncio
import os
from dataclasses import replace

import pytest
from acp.agent.router import build_agent_router

from agent_comms.comms import Comms
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goal_attempts import GoalAttemptStore, UnresolvedAttemptError
from agent_comms.goal_failure_observation import read_failed_turn_projection
from agent_comms.goal_generation import BlockedGeneration, ReadyGeneration
from agent_comms.runtime import RuntimeProxy, socket_path
from delivery_owner_fixture import canonical_agent
from test_backend_native_lifecycle import native_backend as native_backend


async def test_saved_native_acp_failed_goal_remains_passive(native_backend, monkeypatch):
    native = native_backend
    assert (await native.run("Retained history before goal failure"))[-1].ok
    await native.persistent.close()
    history = native.session.read_bytes()
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "response-local/fixture")
    comms = Comms(native.root)
    agent = canonical_agent(
        comms, auto_wake=False, runtime_enabled=True,
        agent_args=[
            "--provider=response-local", "--model=fixture", "--thinking=off", "--offline",
            "--no-extensions", "--no-skills", "--no-context-files",
            "--no-prompt-templates", "--no-tools",
        ],
    )
    route = build_agent_router(agent)
    try:
        async with asyncio.timeout(30):
            created = await route("session/new", {"cwd": str(native.project), "mcpServers": []}, False)
            sid = created.session_id
            comms.registry.register(replace(comms.registry.require(sid), auto_title_pending=False))
            comms.threads.attach_session(sid, str(native.session))
            await route("session/load", {"cwd": str(native.project), "sessionId": sid, "mcpServers": []}, False)
            response = await route("session/prompt", {
                "sessionId": sid,
                "prompt": [{"type": "text", "text": "One explicit ACP input before the goal"}],
            }, False)
            assert response.stop_reason == "end_turn"
            assert len(native.saved_inputs()) == 2
            goal = comms.goals.update_goal(sid, SetGoalAction(text="Exercise one explicit failed attempt"))
            store = agent.turns.goals.open_goal_store()
            store.create_goal(goal.id)
            admission = comms.registry.snapshot().admission_generations[sid]
            key = "acp:retained-unknown"
            agent.inputs.dispositions.record(
                key, seq=None, owner=sid, admission=admission,
                target=sid, text="Never replay this earlier uncertain input",
            )
            unknown = agent.inputs.dispositions.read().rows[key]
            native.provider.status = 503
            # Direct user inputs intentionally do not spend a goal grant.
            # Run the normal autonomous owner entrypoint against the same loaded
            # session; the real backend, input journal and settlement remain live.
            await agent.turns.run_agent_turn(
                sid, sid, "One new failed goal input", autonomous_goal=True
            )
            assert store.snapshot(goal.id).lifecycle == BlockedGeneration()
            assert len(native.saved_inputs()) == 3
            assert native.saved_inputs()[-1]["content"][0]["text"].endswith("One new failed goal input")
            assert native.session.read_bytes().startswith(history)
            assert agent.inputs.dispositions.read().rows[key] == unknown
            registry = comms.registry.snapshot()
            owner = registry.threads[sid]
            before = store.path.read_bytes()
            projected = read_failed_turn_projection(
                store.path, owner=owner, owner_status=registry.statuses[sid],
                admission=registry.admission_generations[sid], pause=comms.goals.goal_pause(sid),
            )
            assert projected.state == "backend_suspended", projected
            assert projected.reason != "missing_binding"
            assert store.path.read_bytes() == before
            with pytest.raises(UnresolvedAttemptError):
                GoalAttemptStore(store.root).resume(goal.id, 1)
            agent.turns.goals.schedule_goal(sid)
            assert not agent.inputs.pending_turns.get(sid)
            assert len(native.saved_inputs()) == 3
            # Explicit owner Retry traverses the socket control, new scheduler,
            # existing wake runner and native journal. The failed/UNKNOWN input
            # above is retained; only one new goal continuation is dispatched.
            native.provider.status = 200
            agent.inputs.auto_wake = True
            blocked = comms.registry.require(sid).goal
            proxy = RuntimeProxy(agent, sid, socket_path(comms.root, os.getpid()))
            try:
                await proxy.request("retry_goal", goal_id=goal.id, expected_revision=blocked.revision)
                await agent.inputs.wake_tasks[sid]
                agent.inputs.auto_wake = False  # bound this isolated journey to one retry
            finally:
                await proxy.close()
            assert store.snapshot(goal.id).number == 3
            assert store.snapshot(goal.id).lifecycle == ReadyGeneration()  # verified native progress
            assert len(native.saved_inputs()) == 4
            assert native.saved_inputs()[-1]["content"][0]["text"].endswith(
                "Continue working toward the active goal."
            )
            assert native.session.read_bytes().startswith(history)
            assert agent.inputs.dispositions.read().rows[key] == unknown
            assert not agent.inputs.pending_turns.get(sid)
            print(f"native_failed_goal_projection={projected.to_primitive()} provider_posts={native.provider.posts} retry_generation=2 next_ready_generation=3 saved_inputs=4")
    finally:
        await agent.shutdown()
