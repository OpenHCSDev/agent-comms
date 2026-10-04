"""Owner Retry is durable immediately and launches only after the current turn."""

import asyncio
import os
from contextlib import aclosing

import pytest

from agent_comms import agent_events as ae
from agent_comms import backend
from agent_comms.acp_extension import GoalChangedUpdate, decode_updates
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import (
    BlockedGoalAction,
    CompletedGoalAction,
    GoalPrecondition,
    ModelInvocable,
    SetGoalAction,
)
from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.goal_generation import CompletedGeneration, ReadyGeneration
from agent_comms.goals import Goal
from agent_comms.runtime import RuntimeProxy, socket_path
from test_backend_native_lifecycle import native_backend

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")


@pytest.mark.parametrize("outcome", ["success", "error", "eof", "exception", "cancel"])
async def test_retry_during_unrelated_turn_is_ready_once_without_overlap(
    native_backend, monkeypatch, outcome
):
    native = native_backend
    await native.author_history()
    async with native.open_owner(runtime_enabled=True, auto_wake=True) as (owner, session):
        await exercise_retry(owner, session, native, monkeypatch, outcome)


async def exercise_retry(owner, session, native, monkeypatch, outcome):
    comms = owner._comms
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
    store = owner.turns.goals.open_goal_store()
    goal = comms.goals.update_goal(
        session, SetGoalAction(text="Finish the blocked objective"), owner_store=store
    )
    reservation = store.reserve(goal.id, 1)
    store.claim_launch(reservation)
    reservation.fail(store, "Previous goal attempt failed")
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
        admission=comms.registry.snapshot().admission_generations[session],
        target=session,
        text="Uncertain old input must never replay",
    )
    old_unknown = owner.inputs.dispositions.read().rows.get("acp:old-unknown")

    native_stream = backend.stream_agent_events

    async def events(*args, **kwargs):
        nonlocal active_backends, max_active_backends
        calls.append(args[2])
        number = len(calls)
        assert number <= 2, "Retry must not schedule a second continuation"
        active_backends += 1
        max_active_backends = max(max_active_backends, active_backends)
        try:
            # Only the external stream's terminal delivery is held/interrupted.
            # Native input IDs, starts, source receipts and phases stay original.
            async with aclosing(native_stream(*args, **kwargs)) as stream:
                async for event in stream:
                    if number == 1 and isinstance(event, ae.InputStarted):
                        entered.set()
                        await release.wait()
                    if number == 2 and isinstance(event, ae.Done):
                        current = comms.registry.require(session).goal
                        assert current.state.active and current.id == goal.id
                        assert store.snapshot(goal.id).number == 2
                        comms.goals.update_goal(
                            session,
                            CompletedGoalAction(expect=GoalPrecondition(goal_id=goal.id)),
                            actor=ModelInvocable,
                        )
                    yield event
                    if number == 1 and isinstance(event, ae.StreamSettled):
                        settled.set()
                        await finish.wait()
                        if outcome == "exception":
                            raise RuntimeError("Current user turn failed")
                        if outcome == "eof":
                            return
                    if number == 2 and isinstance(event, ae.Done):
                        continued.set()
        finally:
            active_backends -= 1

    if outcome == "error":
        native.provider.status = 503
    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
    turn = asyncio.create_task(
        proxy.request("prompt", prompt=[{"type": "text", "text": "Current user request"}])
    )
    try:
        await asyncio.wait_for(entered.wait(), 30)
        assert owner.turns.turn_state(session).busy and session in owner.inputs.backend_inboxes
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
        assert FieldCodec.decode(Goal, accepted[0]["goal"]).state.declared_name == "active"
        assert accepted[0]["goal"]["revision"] == blocked.revision + 1
        generation = GoalAttemptStore(store.root).snapshot(goal.id)
        assert (generation.number, generation.lifecycle, generation.attempt_id) == (
            2,
            ReadyGeneration(),
            None,
        )
        assert store.ready_grant(goal.id, 2)
        await owner.turns.goals.schedule_goal(session)
        assert len(calls) == 1 and not owner.inputs.pending_turns.get(session)
        assert any(
            fact.goal.state.active
            for update in updates
            for fact in decode_updates(update.field_meta)
            if isinstance(fact, GoalChangedUpdate) and fact.goal is not None
        )
        if outcome == "cancel":
            await proxy.request("cancel")
            assert (await turn)["stopReason"] == "cancelled"
            assert comms.registry.require(session).goal.state.declared_name == "paused"
            await owner.turns.goals.schedule_goal(session)
            assert not owner.inputs.pending_turns.get(session) and len(calls) == 1
            assert store.snapshot(goal.id) == generation
        else:
            release.set()
            await asyncio.wait_for(settled.wait(), 30)
            # Native settlement alone is not permission to overlap a still-open stream.
            assert (
                owner.turns.turn_state(session).busy and session in owner.inputs.backend_inboxes
            )
            await owner.turns.goals.schedule_goal(session)
            assert len(calls) == 1 and not owner.inputs.pending_turns.get(session)
            native.provider.status = 200
            finish.set()
            if outcome == "exception":
                with pytest.raises(RuntimeError, match="Current user turn failed"):
                    await turn
            else:
                assert (await turn)["stopReason"] == "end_turn"
            await asyncio.wait_for(continued.wait(), 30)
            await owner.inputs.wake_tasks[session]
            assert len(calls) == 2
            assert comms.registry.require(session).goal.state.declared_name == "completed"
            assert store.snapshot(goal.id).lifecycle == CompletedGeneration()
        assert max_active_backends == 1
        assert owner.inputs.dispositions.read().rows.get("acp:old-unknown") == old_unknown
        assert all("Uncertain old input must never replay" not in prompt for prompt in calls)
    finally:
        release.set()
        finish.set()
        await owner.shutdown()
        await asyncio.gather(turn, return_exceptions=True)
        await proxy.close()


@pytest.mark.parametrize("fence", ["claimed", "reserved", "owner", "origin"])
async def test_busy_retry_keeps_unresolved_attempt_and_owner_fences(native_backend, fence):
    native = native_backend
    await native.author_history()
    async with native.open_owner(runtime_enabled=True) as (owner, session):
        comms = owner._comms
        async with native.original_input(owner, session, "Unrelated turn") as turn:
            store = owner.turns.goals.open_goal_store()
            goal = comms.goals.update_goal(
                session, SetGoalAction(text="Keep attempt authority"), owner_store=store
            )
            reservation = store.reserve(goal.id, 1)
            if fence != "reserved":
                store.claim_launch(reservation)
            if fence in {"owner", "origin"}:
                reservation.fail(store, "Known failed attempt")
            blocked = comms.goals.update_goal(
                session,
                BlockedGoalAction(
                    expect=GoalPrecondition(goal_id=goal.id),
                    block_reason="Owner input required before retry",
                ),
            )
            generation = store.snapshot(goal.id)
            proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
            assert owner.turns.owns_turn(session, turn.turn_id)
            if fence == "owner":
                # Loss is an original registry operation, not an invented PID.
                comms.registry.unregister(session)
            elif fence == "origin":
                owner.turns.goals.pending_goal_origins[session] = goal.id
            try:
                # The original socket admission rejects a stopped owner before
                # request dispatch. Other fences reach the live request owner.
                failure = ConnectionError if fence == "owner" else RuntimeError
                expected = {"owner": "stopped or unavailable", "origin": "origin turn"}.get(fence, "unresolved")
                with pytest.raises(failure, match=expected):
                    await proxy.request("retry_goal", goal_id=goal.id, expected_revision=blocked.revision)
                assert comms.registry.require(session).goal == blocked
                assert store.snapshot(goal.id) == generation
                assert not owner.inputs.pending_turns.get(session)
            finally:
                await proxy.close()
        assert not owner.turns.turn_state(session).busy and not owner.inputs.backend_inboxes
    assert native.provider.posts == 0
