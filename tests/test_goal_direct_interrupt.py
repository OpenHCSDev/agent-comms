"""Provider-free ordinary direct-message turns do not consume goal authority."""

import asyncio
import os
from dataclasses import replace

import pytest

from agent_comms import Thread, wire
from agent_comms import agent_events as ae
from agent_comms.acp import CommsAgent
from agent_comms.declarations import ScheduledTurn
from agent_comms.goal_actions import (
    ActiveGoalAction,
    ClearGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)


async def _owner(tmp_path, monkeypatch, *, standby=False):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _session: None)
    session = (await agent.new_session(str(tmp_path / "owner"))).session_id
    for name in ("outsider", "dependency"):
        comms.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
    goal = comms.update_goal(session, SetGoalAction(text="Wait for the dependency"))
    assert goal is not None
    if standby:
        comms.begin_turn("dependency", "dependency-turn")
        comms.update_goal(
            session,
            StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("dependency",)),
        )
    return comms, agent, session, goal


@pytest.mark.parametrize("standby", [False, True])
async def test_new_nondependency_direct_dm_interrupts_active_goal_without_attempt(
    tmp_path, monkeypatch, standby
):
    comms, agent, session, goal = await _owner(tmp_path, monkeypatch, standby=standby)
    original_wait = comms.goal_wait(session)
    original_goal = comms.registry.require(session).goal
    message = comms.send_message("outsider", session, "A separate question")
    seen = []

    async def fake_events(*args, **kwargs):
        task = args[2]
        seen.append(task)
        assert "ordinary direct-message interruption" in task
        assert f"Persistent goal {goal.id} is parked" in task
        assert (
            f"Goal status: {original_goal.state.declared_name}; revision: {original_goal.revision}"
            in task
        )
        assert f"Objective: {original_goal.text}" in task
        assert f"Progress: {original_goal.progress}" in task
        assert "verify live project state before reporting current PR status" in task
        assert "Use comms_goal with this goal_id" not in task
        assert "A separate question" in task
        native = "a" * 32
        with kwargs["send_boundary"](None, native, task) as allowed:
            assert allowed is True
        row = agent.inputs.dispositions.get(f"bus:{message.seq}")
        assert row is not None
        assert agent.inputs.dispositions.started(
            row["key"], turn_id=row["turn_id"], native_id=native, text=task
        )
        yield ae.InputStarted(id=None)
        yield ae.Chunk(text="Answer to outsider")
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="Answer to outsider")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", fake_events)
    original_schedule = agent.inputs.schedule_wake
    monkeypatch.setattr(agent.inputs, "schedule_wake", lambda _session: None)
    try:
        assert await agent.inputs.drain_owned_inbox(session) == 1
        queued = agent.inputs.pending_turns[session]
        assert len(queued) == 1
        assert queued[0].direct_interrupt_goal_id == goal.id
        assert queued[0].direct_interrupt_goal_revision == original_goal.revision
        assert queued[0].direct_interrupt_wait_id == (
            original_wait.wait_id if original_wait else None
        )
        assert queued[0].goal_wait_id is None
        assert queued[0].direct_interrupt_input_key == f"bus:{message.seq}"
        assert queued[0].direct_interrupt_ticket is not None
        monkeypatch.setattr(agent.inputs, "schedule_wake", original_schedule)
        agent.inputs.schedule_wake(session)
        await asyncio.wait_for(agent.inputs.wake_tasks[session], timeout=3)
        assert len(seen) == 1
        assert agent.inputs.dispositions.status(f"bus:{message.seq}") == "started"
        assert comms.goal_wait(session) == original_wait
        assert comms.registry.require(session).goal == original_goal
        assert not (comms.root / "goal-private").exists()
        assert not agent.inputs.pending_turns.get(session)  # no terminal auto-spin
        # Agent senders have no automatic reply_target; a response is an
        # explicit comms_send, not a fabricated delivery from terminal text.
        assert comms.inbox("outsider") == []
        # Re-seeing the same bus row cannot admit another turn or retry a
        # STARTED/UNKNOWN native attempt, regardless of a presentation cursor.
        agent.inputs.inbox_cursors[session] = message.seq - 1
        assert await agent.inputs.drain_owned_inbox(session) == 1
        assert not agent.inputs.pending_turns.get(session)
        assert len(seen) == 1
    finally:
        await agent.shutdown()


async def test_failed_direct_turn_leaves_goal_and_standby_wait_untouched(tmp_path, monkeypatch):
    comms, agent, session, _goal = await _owner(tmp_path, monkeypatch, standby=True)
    original_wait = comms.goal_wait(session)
    original_goal = comms.registry.require(session).goal
    message = comms.send_message("outsider", session, "What happened?")

    async def failed_events(*args, **kwargs):
        task = args[2]
        native = "c" * 32
        with kwargs["send_boundary"](None, native, task) as allowed:
            assert allowed is True
        row = agent.inputs.dispositions.get(f"bus:{message.seq}")
        assert agent.inputs.dispositions.started(
            row["key"], turn_id=row["turn_id"], native_id=native, text=task
        )
        yield ae.InputStarted(id=None)
        yield ae.StreamSettled()
        yield ae.Done(ok=False, text="Direct turn failed")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", failed_events)
    try:
        await agent.inputs.drain_owned_inbox(session)
        await asyncio.wait_for(agent.inputs.wake_tasks[session], timeout=3)
        assert comms.registry.require(session).goal == original_goal
        assert comms.goal_wait(session) == original_wait
        assert agent.inputs.dispositions.status(f"bus:{message.seq}") == "started"
        assert not (comms.root / "goal-private").exists()
        assert not agent.inputs.pending_turns.get(session)
    finally:
        await agent.shutdown()


async def test_benign_wait_replacement_does_not_strand_queued_interrupt(tmp_path, monkeypatch):
    """A standby refresh must not strand a NEW unattempted DM (seq7248 defect)."""
    comms, agent, session, goal = await _owner(tmp_path, monkeypatch, standby=True)
    old_wait = comms.goal_wait(session)
    message = comms.send_message("outsider", session, "Fresh direct")
    monkeypatch.setattr(agent.inputs, "schedule_wake", lambda _session: None)
    try:
        await agent.inputs.drain_owned_inbox(session)
        assert agent.inputs.pending_turns[session][0].direct_interrupt_wait_id == old_wait.wait_id
        comms.update_goal(
            session,
            StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("dependency",)),
        )
        new_wait = comms.goal_wait(session)
        assert new_wait.wait_id != old_wait.wait_id
        outcome = []

        async def events(*args, **kwargs):
            task = args[2]
            native = "d" * 32
            with kwargs["send_boundary"](None, native, task) as allowed:
                outcome.append(allowed)
            row = agent.inputs.dispositions.get(f"bus:{message.seq}")
            assert agent.inputs.dispositions.started(
                row["key"], turn_id=row["turn_id"], native_id=native, text=task
            )
            yield ae.InputStarted(id=None)
            yield ae.Chunk(text="Still answering")
            yield ae.StreamSettled()
            yield ae.Done(ok=True, text="Still answering")

        monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
        pending = agent.inputs.pending_turns.pop(session)[0]
        current_goal = comms.registry.require(session).goal
        # The dispatcher would rebind at dispatch; mirror that fresh capture
        # exactly, then hold it through the send boundary.
        await agent.turns.run_agent_turn(
            session,
            session,
            pending.prompt,
            origins=(pending.origin,),
            reply_targets=(pending.reply_target,) if pending.reply_target else (),
            direct_interrupt_goal_id=current_goal.id,
            direct_interrupt_goal_revision=current_goal.revision,
            direct_interrupt_wait_id=new_wait.wait_id,
            direct_interrupt_input_key=pending.direct_interrupt_input_key,
            direct_interrupt_ticket=pending.direct_interrupt_ticket,
        )
        # The interrupt ran once under the SAME goal; the refreshed standby
        # wait is preserved untouched, never consumed by this turn.
        assert outcome == [True]
        assert agent.inputs.dispositions.status(f"bus:{message.seq}") == "started"
        assert comms.goal_wait(session) == new_wait
        assert comms.registry.require(session).goal.id == goal.id
        assert comms.registry.require(session).goal.state.active
    finally:
        await agent.shutdown()


async def test_historical_unknown_is_not_replayed_and_dependency_path_is_distinct(
    tmp_path, monkeypatch
):
    comms, agent, session, goal = await _owner(tmp_path, monkeypatch, standby=True)
    monkeypatch.setattr(agent.inputs, "schedule_wake", lambda _session: None)
    try:
        old = comms.send_message("outsider", session, "Already uncertain")
        owner = comms.registry.require(session)
        admission = comms.registry.snapshot().admission_generations[session]
        old_key = agent.inputs.dispositions.bus_key(old, owner)
        assert agent.inputs.dispositions.record(
            old_key,
            seq=old.seq,
            owner=session,
            admission=admission,
            target=old.target,
            text=ScheduledTurn.incoming(old).prompt,
        )
        assert await agent.inputs.drain_owned_inbox(session) == 1
        assert agent.inputs.dispositions.status(old_key) == "unknown"
        assert not agent.inputs.pending_turns.get(session)
        called = []

        async def forbidden_events(*args, **kwargs):
            called.append(True)
            yield ae.Done(ok=True, text="unexpected")

        monkeypatch.setattr("agent_comms.backend.stream_agent_events", forbidden_events)
        await agent.turns.run_agent_turn(
            session,
            session,
            ScheduledTurn.incoming(old).prompt,
            origins=(old,),
            direct_interrupt_goal_id=goal.id,
            direct_interrupt_goal_revision=goal.revision,
            direct_interrupt_input_key=old_key,
            direct_interrupt_ticket="forged-ticket",
        )
        assert called == []
        assert agent.inputs.dispositions.status(old_key) == "unknown"
        dependent = comms.send_message("dependency", session, "Declared answer")
        assert await agent.inputs.drain_owned_inbox(session) == 1
        pending = agent.inputs.pending_turns[session]
        assert len(pending) == 1
        assert pending[0].origin.seq == dependent.seq
        assert pending[0].goal_id == goal.id
        assert pending[0].goal_wait_id == comms.goal_wait(session).wait_id
        assert pending[0].direct_interrupt_goal_id is None
        assert agent.inputs.dispositions.status(old_key) == "unknown"
    finally:
        await agent.shutdown()


async def test_new_owner_admission_refuses_old_direct_input_at_send_boundary(tmp_path, monkeypatch):
    comms, agent, session, goal = await _owner(tmp_path, monkeypatch)
    message = comms.send_message("outsider", session, "Do not cross owner change")
    monkeypatch.setattr(agent.inputs, "schedule_wake", lambda _session: None)
    try:
        await agent.inputs.drain_owned_inbox(session)
        pending = agent.inputs.pending_turns.pop(session)[0]
        current = comms.registry.require(session)
        comms.registry.register(current, comms.registry.status(session), new_owner=True)
        assert comms.registry.require(session).goal == goal
        seen = []

        async def fake_events(*args, **kwargs):
            with kwargs["send_boundary"](None, "b" * 32, args[2]) as allowed:
                seen.append(allowed)
            yield ae.StreamSettled()
            yield ae.Done(ok=False, text="not started")

        monkeypatch.setattr("agent_comms.backend.stream_agent_events", fake_events)
        await agent.turns.run_agent_turn(
            session,
            session,
            pending.prompt,
            origins=(pending.origin,),
            direct_interrupt_goal_id=pending.direct_interrupt_goal_id,
            direct_interrupt_goal_revision=pending.direct_interrupt_goal_revision,
            direct_interrupt_wait_id=pending.direct_interrupt_wait_id,
            direct_interrupt_input_key=pending.direct_interrupt_input_key,
            direct_interrupt_ticket=pending.direct_interrupt_ticket,
        )
        assert seen == [False]
        assert agent.inputs.dispositions.status(f"bus:{message.seq}") == "unknown"
        assert comms.registry.require(session).goal == goal
    finally:
        await agent.shutdown()


async def test_active_backend_does_not_steer_nondependency_dm_into_goal_attempt(
    tmp_path, monkeypatch
):
    comms, agent, session, goal = await _owner(tmp_path, monkeypatch)
    message = comms.send_message("outsider", session, "Wait for an ordinary turn")
    agent.inputs.backend_inboxes[session] = asyncio.Queue()
    monkeypatch.setattr(agent.inputs, "schedule_wake", lambda _session: None)
    try:
        assert await agent.inputs.drain_owned_inbox(session) == 1
        assert agent.inputs.backend_inboxes[session].empty()
        pending = agent.inputs.pending_turns[session]
        assert len(pending) == 1
        assert pending[0].direct_interrupt_goal_id == goal.id
        assert pending[0].origin.seq == message.seq
        assert agent.inputs.dispositions.status(f"bus:{message.seq}") == "unknown"
    finally:
        await agent.shutdown()


async def test_channel_post_does_not_gain_direct_interrupt_authority(tmp_path, monkeypatch):
    comms, agent, session, goal = await _owner(tmp_path, monkeypatch)
    comms.register(Thread("member", frozenset({"ci"}), str(tmp_path), pid=os.getpid()))
    # Matching channel membership is only a passive view. A post alone does
    # not become a direct owner wake just because a goal is active.
    comms.registry.register(
        replace(comms.registry.require(session), tags=frozenset({"ci"})),
        comms.registry.status(session),
    )
    channel = comms.send_message("member", "#ci", "Unmentioned channel update")
    monkeypatch.setattr(agent.inputs, "schedule_wake", lambda _session: None)
    try:
        await agent.inputs.drain_owned_inbox(session)
        assert not agent.inputs.pending_turns.get(session)
        assert comms.registry.require(session).goal == goal
        assert channel.target == "#ci"
    finally:
        await agent.shutdown()


async def test_queue_survives_progress_bump_and_rebinds_at_dispatch(tmp_path, monkeypatch):
    """Queued unattempted DMs survive benign bumps; expectations rebind at dispatch."""
    comms, agent, session, goal = await _owner(tmp_path, monkeypatch)
    original_goal = comms.registry.require(session).goal
    message = comms.send_message("outsider", session, "Question during progress")
    original_schedule = agent.inputs.schedule_wake
    monkeypatch.setattr(agent.inputs, "schedule_wake", lambda _session: None)
    try:
        await agent.inputs.drain_owned_inbox(session)
        queued = agent.inputs.pending_turns[session][0]
        assert queued.direct_interrupt_goal_revision == original_goal.revision
        # Benign same-goal progress bump before dispatch must NOT strand it.
        comms.update_goal(
            session,
            ActiveGoalAction(expect=GoalPrecondition(goal_id=goal.id), progress="normal progress"),
        )
        bumped = comms.registry.require(session).goal
        assert bumped.id == goal.id and bumped.revision == original_goal.revision + 1

        async def events(*args, **kwargs):
            task = args[2]
            native = "f" * 32
            with kwargs["send_boundary"](None, native, task) as allowed:
                assert allowed is True
            row = agent.inputs.dispositions.get(f"bus:{message.seq}")
            assert agent.inputs.dispositions.started(
                row["key"], turn_id=row["turn_id"], native_id=native, text=task
            )
            yield ae.InputStarted(id=None)
            yield ae.Chunk(text="Answer after the bump")
            yield ae.StreamSettled()
            yield ae.Done(ok=True, text="Answer after the bump")

        monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
        monkeypatch.setattr(agent.inputs, "schedule_wake", original_schedule)
        agent.inputs.schedule_wake(session)
        await asyncio.wait_for(agent.inputs.wake_tasks[session], timeout=3)
        # The turn dispatched once with the FRESH revision and stayed started.
        row = agent.inputs.dispositions.get(f"bus:{message.seq}")
        assert row is not None and row["status"] == "started"
        assert not agent.inputs.pending_turns.get(session)
    finally:
        await agent.shutdown()


@pytest.mark.parametrize("mutate", ["revision", "wait"])
async def test_change_after_dispatch_denies_without_retry(tmp_path, monkeypatch, mutate):
    """A revision bump or wait replacement AFTER dispatch denies at the boundary."""
    comms, agent, session, goal = await _owner(tmp_path, monkeypatch, standby=True)
    original_wait = comms.goal_wait(session)
    message = comms.send_message("outsider", session, "Question during standby")
    monkeypatch.setattr(agent.inputs, "schedule_wake", lambda _session: None)
    try:
        await agent.inputs.drain_owned_inbox(session)
        pending = agent.inputs.pending_turns.pop(session)[0]
        current_goal = comms.registry.require(session).goal
        seen = []

        async def events(*args, **kwargs):
            task = args[2]
            # The change happens after dispatch, immediately before the send.
            if mutate == "revision":
                comms.update_goal(
                    session,
                    ActiveGoalAction(
                        expect=GoalPrecondition(goal_id=goal.id), progress="post-dispatch bump"
                    ),
                )
            else:
                comms.update_goal(
                    session,
                    StandbyGoalAction(
                        expect=GoalPrecondition(goal_id=goal.id), wait_for=("dependency",)
                    ),
                )
            with kwargs["send_boundary"](None, "e" * 32, task) as allowed:
                seen.append(allowed)
            yield ae.StreamSettled()
            yield ae.Done(ok=False, text="not started")

        monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
        await agent.turns.run_agent_turn(
            session,
            session,
            pending.prompt,
            origins=(pending.origin,),
            direct_interrupt_goal_id=current_goal.id,
            direct_interrupt_goal_revision=current_goal.revision,
            direct_interrupt_wait_id=original_wait.wait_id,
            direct_interrupt_input_key=pending.direct_interrupt_input_key,
            direct_interrupt_ticket=pending.direct_interrupt_ticket,
        )
        # Deny the stale expectation once; no retry, no replay.
        assert seen == [False]
        row = agent.inputs.dispositions.get(f"bus:{message.seq}")
        assert row["status"] == "unknown" and row["native_id"] is None
    finally:
        await agent.shutdown()


@pytest.mark.parametrize("terminal", ["clear", "paused"])
async def test_goal_cleared_or_paused_drops_queued_interrupt_without_crash(
    tmp_path, monkeypatch, terminal
):
    """No active goal: queued ordinary interrupts are discarded, never dispatched."""
    comms, agent, session, goal = await _owner(tmp_path, monkeypatch)
    message = comms.send_message("outsider", session, "Question before the change")
    original_schedule = agent.inputs.schedule_wake
    monkeypatch.setattr(agent.inputs, "schedule_wake", lambda _session: None)
    try:
        await agent.inputs.drain_owned_inbox(session)
        assert agent.inputs.pending_turns[session][0].direct_interrupt_goal_id == goal.id
        if terminal == "clear":
            comms.update_goal(
                session,
                ClearGoalAction(expect=GoalPrecondition(goal_id=goal.id)),
                actor=OwnerInvocable,
            )
        else:
            comms.update_goal(
                session,
                PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)),
                actor=OwnerInvocable,
            )
        called = []

        async def forbidden_events(*args, **kwargs):
            called.append(True)
            yield ae.Done(ok=True, text="unexpected")

        monkeypatch.setattr("agent_comms.backend.stream_agent_events", forbidden_events)
        monkeypatch.setattr(agent.inputs, "schedule_wake", original_schedule)
        agent.inputs.schedule_wake(session)
        if agent.inputs.wake_tasks.get(session):
            await asyncio.wait_for(agent.inputs.wake_tasks[session], timeout=3)
        # No turn, no crash, no ticket reuse; the row stays durably UNKNOWN.
        assert called == []
        row = agent.inputs.dispositions.get(f"bus:{message.seq}")
        assert row is not None and row["status"] == "unknown" and row["native_id"] is None
        assert not agent.inputs.pending_turns.get(session)
        assert not agent.inputs.direct_interrupt_tickets.get(session, {})
    finally:
        await agent.shutdown()
