"""Standby liveness preserves goal authority and never replays an input."""

import asyncio
import os
import threading
from dataclasses import replace

import pytest

from agent_comms import agent_events as ae
from agent_comms.schedule_rules import WakeScheduleCheck
from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.goal_actions import (
    EditGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.goal_waits import GoalWait, GoalWaits
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread
from agent_comms.transcript_updates import TurnTranscriptUpdate
from delivery_owner_fixture import canonical_agent
from goal_owner_fixture import activate_empty_source, canonical_goal_wire
from test_backend_native_lifecycle import native_backend as native_backend


@pytest.fixture
async def retained_native_acp_owner(native_backend, monkeypatch):
    """Actual ACP owner and native producer; only the localhost provider is controlled."""
    native = native_backend
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "response-local/fixture")
    comms = Comms(native.root)
    agent = canonical_agent(
        comms,
        agent_bin="pi",
        runtime_enabled=True,
        auto_wake=False,
        agent_args=[
            "--provider=response-local",
            "--model=fixture",
            "--thinking=off",
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--no-prompt-templates",
            "--no-tools",
        ],
    )
    try:
        child = (await agent.new_session(str(native.project))).session_id
        drain = agent.inputs.drain_tasks.pop(child)
        drain.cancel()
        await asyncio.gather(drain, return_exceptions=True)
        comms.registry.register(replace(comms.registry.require(child), auto_title_pending=False))
        comms.threads.attach_session(child, str(native.session))
        yield native, comms, agent, child
    finally:
        await agent.shutdown()


def _thread(comms, name, worktree):
    comms.registry.declare(
        Thread(
            name, frozenset(), str(worktree), process_identity=ProcessIdentity.capture(os.getpid())
        )
    )


def _begin(comms, name, turn_id):
    lease = comms.agents.begin_turn(name, turn_id).turn_lease
    comms._test_leases[name] = lease
    return lease


def _finish(comms, name, turn_id):
    canonical = comms.registry.canonical_name(name)
    lease = next(
        lease
        for lease in comms._test_leases.values()
        if comms.registry.canonical_name(lease.identity.incarnation.name) == canonical
    )
    return comms.agents.finish_turn(lease)


def _waiting(tmp_path, *, second=False):
    comms = canonical_goal_wire(tmp_path / "wire")
    comms._test_leases = {}
    _thread(comms, "owner", tmp_path)
    _thread(comms, "child", tmp_path)
    _begin(comms, "child", "child-turn")
    if second:
        _thread(comms, "other", tmp_path)
        _begin(comms, "other", "other-turn")
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Review the delegated work"))
    assert goal is not None
    targets = ["child", "other"] if second else ["child"]
    comms.goals.update_goal(
        "owner", StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=targets)
    )
    return comms, goal


def test_idle_target_refusal_is_side_effect_free(tmp_path):
    comms = canonical_goal_wire(tmp_path / "wire")
    _thread(comms, "owner", tmp_path)
    _thread(comms, "child", tmp_path)
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Work"))
    assert goal is not None
    authoritative = ("registry.json", "goal_waits.json", "input_dispositions.json")
    before = {
        name: (comms.root / name).read_bytes() if (comms.root / name).exists() else None
        for name in authoritative
    }
    with pytest.raises(ValueError, match="No declared dependency has an active turn.*@child"):
        comms.goals.update_goal(
            "owner",
            StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("child",)),
        )
    assert {
        name: (comms.root / name).read_bytes() if (comms.root / name).exists() else None
        for name in authoritative
    } == before
    assert comms.registry.require("owner").goal == goal
    assert comms.goals.goal_wait("owner") is None


def test_optional_reply_read_failure_after_terminal_commit_never_releases_or_fails(
    tmp_path, monkeypatch
):
    comms, goal = _waiting(tmp_path)
    fence = _finish(comms, "child", "child-turn")
    assert fence is not None and comms.registry.require("child").active_turn is None
    original = GoalWait.has_reply

    def unavailable(*_args, **_kwargs):
        raise OSError("injected optional direct-reply read failure")

    monkeypatch.setattr(GoalWait, "has_reply", unavailable)
    assert comms.goals.release_waits_after_terminal_turn(fence) == ()
    assert comms.registry.require("child").active_turn is None
    assert comms.registry.require("owner").goal.state.active
    assert comms.registry.require("owner").goal.id == goal.id
    assert comms.goals.goal_wait("owner") is not None
    monkeypatch.setattr(GoalWait, "has_reply", original)
    assert comms.goals.release_waits_after_terminal_turn(fence) == ("owner",)


def test_authoritative_goal_write_errors_are_not_swallowed_by_optional_read_guard(
    tmp_path, monkeypatch
):
    comms, _goal = _waiting(tmp_path)
    fence = _finish(comms, "child", "child-turn")
    original_register = comms.registry.register

    def denied(*_args, **_kwargs):
        raise OSError("injected authoritative goal write failure")

    monkeypatch.setattr(comms.registry, "register", denied)
    with pytest.raises(OSError, match="authoritative goal write failure"):
        comms.goals.release_waits_after_terminal_turn(fence)
    assert comms.registry.require("owner").goal.state.active
    assert comms.goals.goal_wait("owner") is not None
    monkeypatch.setattr(comms.registry, "register", original_register)
    assert comms.goals.release_waits_after_terminal_turn(fence) == ("owner",)


def test_mixed_targets_project_idle_without_suppressing_active_alternative(tmp_path):
    comms, goal = _waiting(tmp_path, second=True)
    other_fence = _finish(comms, "other", "other-turn")
    execution = comms.goals.goal_execution("owner")
    assert [target.name for target in execution.inactive_wait_for] == ["other"]
    assert "no active turn: @other" in execution.presentation("owner").summary
    assert comms.goals.release_waits_after_terminal_turn(other_fence) == ()
    assert comms.registry.require("owner").goal.state.active
    assert comms.goals.goal_wait("owner") is not None
    assert goal.id == comms.registry.require("owner").goal.id


def test_last_target_finishes_silently_releases_once_and_reopens(tmp_path):
    comms, goal = _waiting(tmp_path, second=True)
    other_fence = _finish(comms, "other", "other-turn")
    assert comms.goals.release_waits_after_terminal_turn(other_fence) == ()
    child_fence = _finish(comms, "child", "child-turn")
    assert comms.goals.release_waits_after_terminal_turn(child_fence) == ("owner",)
    reopened = Comms(comms.root)
    continued = reopened.registry.require("owner").goal
    assert continued.id == goal.id and continued.state.declared_name == "active"
    assert continued.revision == goal.revision + 2
    assert "without a qualifying direct reply" in continued.progress
    assert reopened.goals.goal_execution("owner").state.value == "runnable"
    assert reopened.goals.goal_wait("owner") is None
    assert reopened.registry.require("owner").goal.state.pause_source is None
    assert reopened.goals.release_waits_after_terminal_turn(child_fence) == ()
    assert reopened.registry.require("owner").goal == continued
    assert not (comms.root / "goal-private").exists()


@pytest.mark.parametrize("notice", [False, True])
def test_only_substantive_direct_reply_prevents_release(tmp_path, notice):
    comms, _goal = _waiting(tmp_path)
    comms.messaging.send_message(
        "child", "owner", "Result" if not notice else "Failure notice", notice=notice
    )
    child_fence = _finish(comms, "child", "child-turn")
    changed = comms.goals.release_waits_after_terminal_turn(child_fence)
    assert changed == (("owner",) if notice else ())
    assert comms.registry.require("owner").goal.state.declared_name == "active"
    assert (comms.goals.goal_wait("owner") is None) is notice


def test_renamed_target_keeps_incarnation_but_replacement_fails_closed(tmp_path):
    comms, goal = _waiting(tmp_path)
    comms.registry.rename("child", "renamed-child")
    child_fence = _finish(comms, "renamed-child", "child-turn")
    assert comms.goals.release_waits_after_terminal_turn(child_fence) == ("owner",)
    assert comms.registry.require("owner").goal.state.declared_name == "active"
    assert comms.registry.require("owner").goal.id == goal.id
    # A later registered incarnation must not report an older turn's result.
    assert (
        comms.goals.release_waits_after_terminal_turn(
            replace(
                child_fence,
                identity=replace(
                    child_fence.identity,
                    incarnation=replace(
                        child_fence.identity.incarnation,
                        created_at=child_fence.identity.incarnation.created_at + 1,
                    ),
                ),
            )
        )
        == ()
    )


def test_progress_write_precedes_wait_clear_on_crash_and_stays_in_standby(tmp_path, monkeypatch):
    comms, goal = _waiting(tmp_path)
    child_fence = _finish(comms, "child", "child-turn")

    original_clear = GoalWaits.clear

    def crash_before_wait_clear(*_args, **_kwargs):
        raise OSError("injected crash after progress registry")

    monkeypatch.setattr(GoalWaits, "clear", crash_before_wait_clear)
    with pytest.raises(OSError, match="injected crash"):
        comms.goals.release_waits_after_terminal_turn(child_fence)
    reopened = Comms(comms.root)
    assert reopened.registry.require("owner").goal.state.declared_name == "active"
    assert reopened.registry.require("owner").goal.id == goal.id
    assert reopened.goals.goal_execution("owner").state.value == "standby"
    assert reopened.goals.goal_wait("owner") is not None
    assert reopened.registry.status("owner") == RunningThreadStatus()
    monkeypatch.setattr(GoalWaits, "clear", original_clear)
    assert reopened.goals.recover_closed_goal_wait("owner") == ("owner",)
    assert reopened.goals.goal_execution("owner").state.value == "runnable"


def test_replaced_target_cannot_report_for_old_incarnation(tmp_path):
    comms, goal = _waiting(tmp_path)
    old = comms.registry.require("child")
    old_fence = _finish(comms, "child", "child-turn")
    comms.registry.unregister("child")
    comms.registry.remove("child")
    _thread(comms, "child", tmp_path)
    replacement = comms.registry.require("child")
    assert replacement.created_at != old.created_at
    assert comms.goals.release_waits_after_terminal_turn(old_fence) == ()
    execution = comms.goals.goal_execution("owner")
    assert [target.name for target in execution.inactive_wait_for] == ["child"]
    assert comms.registry.require("owner").goal.id == goal.id
    # Removal/replacement has no subscribed terminal hook in this prototype.
    assert comms.registry.require("owner").goal.state.active


def test_owner_pause_still_prevents_quiet_dependency_release(tmp_path):
    comms, goal = _waiting(tmp_path)
    paused = comms.goals.update_goal(
        "owner", PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)), actor=OwnerInvocable
    )
    assert paused is not None
    fence = _finish(comms, "child", "child-turn")
    assert comms.goals.release_waits_after_terminal_turn(fence) == ()
    assert comms.registry.require("owner").goal == paused
    assert comms.registry.require("owner").goal.state.pause_source.declared_name == "owner"
    assert comms.goals.goal_wait("owner") is None


def test_reply_arriving_after_idle_check_stays_visible_without_model_start(tmp_path, monkeypatch):
    comms, _goal = _waiting(tmp_path)
    child_fence = _finish(comms, "child", "child-turn")
    checked = threading.Event()
    sent = threading.Event()

    def late_send():
        checked.set()
        comms.messaging.send_message("child", "owner", "Late direct reply")
        sent.set()

    real_history = GoalWait.has_reply
    sender = threading.Thread(target=late_send)

    def interpose(*args, **kwargs):
        sender.start()
        assert checked.wait(timeout=2)
        return real_history(*args, **kwargs)

    monkeypatch.setattr(GoalWait, "has_reply", interpose)
    assert comms.goals.release_waits_after_terminal_turn(child_fence) == ("owner",)
    sender.join(timeout=2)
    assert sent.is_set()
    assert comms.registry.require("owner").goal.state.declared_name == "active"
    assert [message.body for message in comms.bus.inbox("owner")] == ["Late direct reply"]
    assert comms.goals.goal_wait("owner") is None
    assert not (comms.root / "goal-private").exists()


def test_terminal_hook_rechecks_goal_and_owner_identity(tmp_path):
    comms, goal = _waiting(tmp_path)
    child_fence = _finish(comms, "child", "child-turn")
    newer = comms.goals.update_goal(
        "owner",
        EditGoalAction(expect=GoalPrecondition(goal_id=goal.id), text="new exact objective"),
    )
    assert newer is not None
    # Goal text edits preserve wait; the exact current revision is updated, not lost.
    assert comms.goals.release_waits_after_terminal_turn(child_fence) == ("owner",)
    assert comms.registry.require("owner").goal.revision == newer.revision + 1
    assert comms.registry.require("owner").goal.text == newer.text
    assert comms.goals.release_waits_after_terminal_turn(child_fence) == ()


def test_stopped_dependency_cannot_be_declared_live(tmp_path):
    comms = canonical_goal_wire(tmp_path / "wire")
    _thread(comms, "owner", tmp_path)
    _thread(comms, "child", tmp_path)
    comms.agents.begin_turn("child", "work")
    child = comms.registry.require("child")
    comms.registry.register(replace(child, active_turn=None), StoppedThreadStatus())
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Review"))
    assert goal is not None
    with pytest.raises(ValueError, match="No declared dependency has an active turn"):
        comms.goals.update_goal(
            "owner",
            StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("child",)),
        )


def test_delayed_old_finish_cannot_release_new_active_same_id(tmp_path):
    comms, _goal = _waiting(tmp_path)
    old_claim = comms._test_leases["child"]
    old_fence = _finish(comms, "child", "child-turn")
    new_claim = _begin(comms, "child", "child-turn")
    assert new_claim.identity.generation == old_claim.identity.generation + 1
    active = comms.registry.require("child").active_turn
    assert comms.agents.finish_turn(old_claim) is None
    assert comms.registry.require("child").active_turn == active
    assert comms.goals.release_waits_after_terminal_turn(old_fence) == ()
    assert comms.registry.require("owner").goal.state.active
    assert comms.goals.goal_wait("owner") is not None
    new_fence = comms.agents.finish_turn(new_claim)
    assert new_fence is not None and new_fence.identity.generation == new_claim.identity.generation
    assert comms.goals.release_waits_after_terminal_turn(new_fence) == ("owner",)


@pytest.mark.parametrize("reuse_turn_id", [False, True])
def test_delayed_old_terminal_cannot_release_newer_turn_before_reply(tmp_path, reuse_turn_id):
    comms, goal = _waiting(tmp_path)
    old_fence = _finish(comms, "child", "child-turn")
    assert old_fence is not None
    newer_id = "child-turn" if reuse_turn_id else "new-child-turn"
    _begin(comms, "child", newer_id)
    new_fence = _finish(comms, "child", newer_id)
    assert new_fence is not None
    assert new_fence.identity.generation == old_fence.identity.generation + 1
    assert comms.goals.release_waits_after_terminal_turn(old_fence) == ()
    assert comms.registry.require("owner").goal.state.active
    assert comms.goals.goal_wait("owner") is not None
    # The newer turn publishes after the OLD callback, before its own callback.
    comms.messaging.send_message("child", "owner", "New turn's substantive answer")
    assert comms.goals.release_waits_after_terminal_turn(new_fence) == ()
    assert comms.registry.require("owner").goal.state.active
    assert comms.registry.require("owner").goal.id == goal.id
    assert comms.goals.goal_wait("owner") is not None
    assert not (comms.root / "goal-private").exists()


def test_newer_silent_terminal_releases_waiter(tmp_path):
    comms, _goal = _waiting(tmp_path)
    old_fence = _finish(comms, "child", "child-turn")
    _begin(comms, "child", "new-child-turn")
    new_fence = _finish(comms, "child", "new-child-turn")
    assert comms.goals.release_waits_after_terminal_turn(old_fence) == ()
    assert comms.goals.release_waits_after_terminal_turn(new_fence) == ("owner",)
    assert comms.goals.goal_wait("owner") is None


def test_terminal_fence_survives_rename_not_stop_or_metadata_edit(tmp_path):
    comms, _goal = _waiting(tmp_path)
    fence = _finish(comms, "child", "child-turn")
    assert fence is not None
    child = comms.registry.require("child")
    comms.registry.register(replace(child, title="New title"), RunningThreadStatus())
    comms.registry.rename("child", "renamed-child")
    assert (
        comms.goals.release_waits_after_terminal_turn(
            replace(fence, identity=replace(fence.identity, generation=2))
        )
        == ()
    )
    assert comms.goals.release_waits_after_terminal_turn(fence) == ("owner",)

    another, _goal = _waiting(tmp_path / "stop")
    stale = _finish(another, "child", "child-turn")
    child = another.registry.require("child")
    another.registry.unregister("child")
    another.registry.register(replace(child, active_turn=None), RunningThreadStatus())
    assert another.goals.release_waits_after_terminal_turn(stale) == ()
    assert another.registry.require("owner").goal.state.active


def test_unbound_wait_is_rejected_without_replacing_current_wait(tmp_path):
    comms, _goal = _waiting(tmp_path)
    wait = comms.goals.goal_wait("owner")
    before = GoalWaits(comms.root / "goal_waits.json").path.read_bytes()
    with pytest.raises(ValueError, match="aligned target turns"):
        GoalWaits(comms.root / "goal_waits.json").record(replace(wait, target_turn_generations=()))
    assert GoalWaits(comms.root / "goal_waits.json").path.read_bytes() == before


async def test_acp_optional_reply_read_failure_after_settled_does_not_fail_done(
    retained_native_acp_owner, monkeypatch
):
    native, comms, agent, child = retained_native_acp_owner
    _thread(comms, "owner", native.project)
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Await child"))
    assert goal is not None
    terminal = []
    original_emit = agent._emit_event

    async def capture_emit(session_id, event, client=None, **kwargs):
        if isinstance(event, ae.InputStarted):
            comms.goals.update_goal(
                "owner",
                StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=(child,)),
            )
        if isinstance(event, TurnTranscriptUpdate) and not event.state.busy:
            terminal.append("settled")
        await original_emit(session_id, event, client, **kwargs)

    original_history_page = GoalWait.has_reply

    def unavailable(*args, **kwargs):
        if not terminal:
            return original_history_page(*args, **kwargs)
        assert terminal == ["settled"]
        assert comms.registry.require(child).active_turn is None
        raise OSError("injected optional direct-reply read failure")

    monkeypatch.setattr(agent, "_emit_event", capture_emit)
    monkeypatch.setattr(GoalWait, "has_reply", unavailable)
    async with asyncio.timeout(20):
        await agent.turns.run_agent_turn(child, child, "Finish work")
    assert terminal == ["settled"]
    assert comms.registry.require("owner").goal.state.active
    assert comms.goals.goal_wait("owner") is not None
    assert comms.registry.require(child).active_turn is None
    assert native.provider.posts == len(native.saved_inputs()) == 1


async def test_quiet_dependency_finish_schedules_the_still_active_goal(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = canonical_goal_wire(tmp_path / "wire")
    root_id = comms.messaging.initialize_private_initial_protocol()
    agent = CommsAgent(
        comms,
        agent_bin="pi",
        runtime_enabled=True,
        auto_wake=False,
        private_nk_native_package=tmp_path,
        private_nk_wire_root_id=root_id,
    )
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _session: None)
    owner = (await agent.new_session(str(tmp_path / "owner"))).session_id
    child = (await agent.new_session(str(tmp_path / "child"))).session_id
    activate_empty_source(agent)
    goal = comms.goals.update_goal(
        owner, SetGoalAction(text="Review child work"), owner_store=agent.turns.goals.open_goal_store()
    )
    assert goal is not None
    wakes = []
    monkeypatch.setattr(WakeScheduleCheck, "schedule", lambda check: wakes.append(check.session_id))
    try:
        lease = comms.agents.begin_turn(child, "child-turn").turn_lease
        comms.goals.update_goal(
            owner, StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=(child,))
        )
        assert not agent.inputs.pending_turns.get(owner)
        fence = comms.agents.finish_turn(lease)
        assert comms.goals.release_waits_after_terminal_turn(fence) == (owner,)
        assert comms.registry.require(owner).goal.state.active
        assert comms.goals.goal_wait(owner) is None
        await agent.turns.goals.schedule_goal(owner)
        assert wakes == [owner]
        assert [turn.goal_id for turn in agent.inputs.pending_turns[owner]] == [goal.id]
    finally:
        await agent.shutdown()


async def test_acp_delayed_old_callback_after_new_finish_before_reply(
    retained_native_acp_owner, monkeypatch
):
    native, comms, agent, child = retained_native_acp_owner
    _thread(comms, "owner", native.project)
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Await child"))
    assert goal is not None
    old_settled, release_old = asyncio.Event(), asyncio.Event()
    real_emit = agent._emit_event

    async def delayed_emit(session_id, event, client=None, **kwargs):
        if isinstance(event, ae.InputStarted):
            comms.goals.update_goal(
                "owner",
                StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=(child,)),
            )
        if (
            isinstance(event, TurnTranscriptUpdate)
            and not event.state.busy
            and not old_settled.is_set()
        ):
            old_settled.set()
            await release_old.wait()
        await real_emit(session_id, event, client, **kwargs)

    monkeypatch.setattr(agent, "_emit_event", delayed_emit)
    old_task = asyncio.create_task(agent.turns.run_agent_turn(child, child, "Finish work"))
    try:
        await asyncio.wait_for(old_settled.wait(), 20)
        assert comms.registry.require(child).active_turn is None
        new_claim = comms.agents.begin_turn(child, "new-child-turn").turn_lease
        new_fence = comms.agents.finish_turn(new_claim)
        release_old.set()
        await asyncio.wait_for(old_task, 5)
        assert comms.registry.require("owner").goal.state.active
        assert comms.goals.goal_wait("owner") is not None
        comms.messaging.send_message(child, "owner", "New turn's result")
        assert comms.goals.release_waits_after_terminal_turn(new_fence) == ()
        assert comms.registry.require("owner").goal.state.active
        assert native.provider.posts == len(native.saved_inputs()) == 1
    finally:
        release_old.set()
        if not old_task.done():
            old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)


@pytest.mark.parametrize("reply", [False, True])
async def test_acp_settled_is_not_terminal_reply_and_never_admits_waiter_model(
    retained_native_acp_owner, monkeypatch, reply
):
    native, comms, agent, child = retained_native_acp_owner
    native.provider.text = "Reported" if reply else ""
    _thread(comms, "owner", native.project)
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Await child"))
    assert goal is not None
    observed = []
    real_emit = agent._emit_event

    async def observe(session_id, event, client=None, **kwargs):
        if isinstance(event, ae.InputStarted):
            observed.append("started")
            comms.goals.update_goal(
                "owner",
                StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=(child,)),
            )
        if isinstance(event, ae.StreamSettled):
            observed.append("native-settled")
            assert comms.registry.require(child).active_turn is not None
            assert comms.registry.require("owner").goal.state.active
            assert comms.goals.goal_wait("owner") is not None
        await real_emit(session_id, event, client, **kwargs)

    monkeypatch.setattr(agent, "_emit_event", observe)
    async with asyncio.timeout(20):
        await agent.turns.run_agent_turn(
            child, child, "Finish work", reply_targets=("owner",) if reply else ()
        )
    assert observed == ["started", "native-settled"]
    assert native.provider.posts == len(native.saved_inputs()) == 1
    assert not agent.inputs.pending_turns.get("owner")
    assert not (comms.root / "goal-private").exists()
    assert comms.registry.require(child).active_turn is None
    if reply:
        assert comms.registry.require("owner").goal.state.active
        assert comms.goals.goal_wait("owner") is not None
        assert any(m.body == "Reported" for m in comms.bus.inbox("owner"))
    else:
        assert comms.registry.require("owner").goal.state.active
        assert comms.goals.goal_wait("owner") is None
