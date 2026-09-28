"""Owner Retry is durable immediately and launches only after the current turn."""

import asyncio
import os
from dataclasses import replace

import pytest

from agent_comms import agent_events as ae
from agent_comms.acp import CommsAgent
from agent_comms.comms import wire
from agent_comms.goal_actions import (
    BlockedGoalAction,
    CompletedGoalAction,
    GoalPrecondition,
    ModelInvocable,
    SetGoalAction,
)
from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.runtime import RuntimeProxy, socket_path

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")


@pytest.mark.parametrize("outcome", ["success", "error", "eof", "exception", "cancel"])
async def test_retry_during_unrelated_turn_is_ready_once_without_overlap(
    tmp_path, monkeypatch, outcome
):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    entered, release = asyncio.Event(), asyncio.Event()
    settled, finish = asyncio.Event(), asyncio.Event()
    continued = asyncio.Event()
    calls = []
    active_backends = 0
    max_active_backends = 0
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    owner.on_connect(Client())
    session = (await owner.new_session(str(tmp_path / "project"))).session_id
    store = owner.turns.open_goal_store()
    goal = comms.goals.update_goal(
        session, SetGoalAction(text="Finish the blocked objective"), owner_store=store
    )
    reservation = store.reserve(goal.id, 1)
    store.claim_launch(reservation)
    store.record_failed(reservation, "Previous goal attempt failed")
    blocked = comms.goals.update_goal(
        session,
        BlockedGoalAction(
            expect=GoalPrecondition(goal_id=goal.id), block_reason="Previous goal attempt failed"
        ),
    )
    owner.inputs.dispositions.record(
        "acp:old-unknown",
        seq=None,
        owner=session,
        admission=1,
        target=session,
        text="Uncertain old input must never replay",
    )
    old_unknown = owner.inputs.dispositions.get("acp:old-unknown")

    async def events(*args, **kwargs):
        nonlocal active_backends, max_active_backends
        calls.append(args[2])
        number = len(calls)
        assert number <= 2, "Retry must not schedule a second continuation"
        active_backends += 1
        max_active_backends = max(max_active_backends, active_backends)
        try:
            native_id = f"{number:032x}"
            with kwargs["send_boundary"](None, native_id, args[2]) as allowed:
                assert allowed is True
            assert kwargs["native_start"](None, native_id, args[2])
            yield ae.InputStarted(id=None)
            if number == 1:
                entered.set()
                await release.wait()
                yield ae.StreamSettled()
                settled.set()
                await finish.wait()
                if outcome == "exception":
                    raise RuntimeError("Current user turn failed")
                if outcome != "eof":
                    yield ae.Done(ok=outcome == "success", text="Current turn ended")
            else:
                current = comms.registry.require(session).goal
                assert current.state.active and current.id == goal.id
                assert store.snapshot(goal.id).number == 2
                comms.goals.update_goal(
                    session,
                    CompletedGoalAction(expect=GoalPrecondition(goal_id=goal.id)),
                    actor=ModelInvocable,
                )
                yield ae.ToolEnd(id="report", name="alternate_goal_report", ok=True)
                yield ae.StreamSettled()
                yield ae.Done(ok=True, text="Goal completed")
                continued.set()
        finally:
            active_backends -= 1

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
    turn = asyncio.create_task(
        proxy.request("prompt", prompt=[{"type": "text", "text": "Current user request"}])
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert session in owner.turns.active_turns and session in owner.inputs.backend_inboxes
        results = await asyncio.gather(
            *(
                proxy.request("retry_goal", goal_id=goal.id, expected_revision=blocked.revision)
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        accepted = [result for result in results if isinstance(result, dict)]
        rejected = [result for result in results if isinstance(result, RuntimeError)]
        assert len(accepted) == len(rejected) == 1
        assert "changed" in str(rejected[0])
        assert accepted[0]["goal"]["status"] == "active"
        assert accepted[0]["goal"]["revision"] == blocked.revision + 1
        generation = GoalAttemptStore(store.root).snapshot(goal.id)
        assert (generation.number, generation.state, generation.attempt_id) == (2, "ready", None)
        assert store.ready_grant(goal.id, 2)
        owner.turns.schedule_goal(session)
        assert len(calls) == 1 and not owner.inputs.pending_turns.get(session)
        assert any(
            (getattr(update, "field_meta", None) or {})
            .get("agentComms", {})
            .get("goal", {})
            .get("status")
            == "active"
            for update in updates
            if (getattr(update, "field_meta", None) or {}).get("agentComms", {}).get("goal")
        )
        if outcome == "cancel":
            await proxy.request("cancel")
            assert (await turn)["stopReason"] == "cancelled"
            assert comms.registry.require(session).goal.state.declared_name == "paused"
            owner.turns.schedule_goal(session)
            assert not owner.inputs.pending_turns.get(session) and len(calls) == 1
            assert store.snapshot(goal.id) == generation
        else:
            release.set()
            await asyncio.wait_for(settled.wait(), 3)
            # Native settlement alone is not permission to overlap a still-open stream.
            assert (
                session not in owner.turns.active_turns and session in owner.inputs.backend_inboxes
            )
            owner.turns.schedule_goal(session)
            assert len(calls) == 1 and not owner.inputs.pending_turns.get(session)
            finish.set()
            if outcome == "exception":
                with pytest.raises(RuntimeError, match="Current user turn failed"):
                    await turn
            else:
                assert (await turn)["stopReason"] == "end_turn"
            await asyncio.wait_for(continued.wait(), 5)
            await owner.inputs.wake_tasks[session]
            assert len(calls) == 2
            assert comms.registry.require(session).goal.state.declared_name == "completed"
            assert store.snapshot(goal.id).state == "completed"
        assert max_active_backends == 1
        assert owner.inputs.dispositions.get("acp:old-unknown") == old_unknown
        assert all("Uncertain old input must never replay" not in prompt for prompt in calls)
    finally:
        release.set()
        finish.set()
        await owner.shutdown()
        await asyncio.gather(turn, return_exceptions=True)
        await proxy.close()


@pytest.mark.parametrize("fence", ["claimed", "reserved", "owner", "origin"])
async def test_busy_retry_keeps_unresolved_attempt_and_owner_fences(tmp_path, monkeypatch, fence):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(comms, agent_bin="pi", runtime_enabled=True, auto_wake=False)
    monkeypatch.setattr(owner.inputs, "ensure_live_drain", lambda _: None)
    session = (await owner.new_session(str(tmp_path / "project"))).session_id
    store = owner.turns.open_goal_store()
    goal = comms.goals.update_goal(
        session, SetGoalAction(text="Keep attempt authority"), owner_store=store
    )
    reservation = store.reserve(goal.id, 1)
    if fence != "reserved":
        store.claim_launch(reservation)
    if fence in {"owner", "origin"}:
        store.record_failed(reservation, "Known failed attempt")
    blocked = comms.goals.update_goal(
        session,
        BlockedGoalAction(
            expect=GoalPrecondition(goal_id=goal.id),
            block_reason="Owner input required before retry",
        ),
    )
    generation = store.snapshot(goal.id)
    owner.turns.active_turns[session] = "unrelated-turn"
    owner.inputs.backend_inboxes[session] = asyncio.Queue()
    proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
    if fence == "owner":
        # Call the actual owner handler after identity changed, without redirecting the proxy.
        original = owner.sessions.require

        def replaced_owner(session_id):
            comms.registry.register(
                replace(comms.registry.require(session), pid=os.getpid() + 100000)
            )
            return original(session_id)

        monkeypatch.setattr(owner.sessions, "require", replaced_owner)
    elif fence == "origin":
        owner.turns.pending_goal_origins[session] = goal.id
    try:
        expected = {"owner": "owner changed", "origin": "origin turn"}.get(fence, "unresolved")
        with pytest.raises(RuntimeError, match=expected):
            await proxy.request("retry_goal", goal_id=goal.id, expected_revision=blocked.revision)
        assert comms.registry.require(session).goal == blocked
        assert store.snapshot(goal.id) == generation
        assert not owner.inputs.pending_turns.get(session)
    finally:
        owner.turns.active_turns.clear()
        owner.inputs.backend_inboxes.clear()
        await owner.shutdown()
        await proxy.close()
