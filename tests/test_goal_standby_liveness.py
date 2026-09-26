"""Standby liveness preserves goal authority and never replays an input."""

import asyncio
import os
import threading
from dataclasses import replace

import pytest

from agent_comms import Thread, wire
from agent_comms.acp import CommsAgent
from agent_comms.declarations import ThreadStatus
from agent_comms.goal_waits import GoalWaits
from agent_comms.operations import Comms


def _thread(comms, name, worktree):
    comms.register(Thread(name, frozenset(), str(worktree), pid=os.getpid()))


def _begin(comms, name, turn_id):
    claim = comms.begin_turn(name, turn_id)
    comms._test_claims[name] = claim
    return claim


def _finish(comms, name, turn_id):
    canonical = comms.registry.canonical_name(name)
    claim = next(
        claim
        for claim in comms._test_claims.values()
        if comms.registry.canonical_name(claim.name) == canonical
    )
    return comms.finish_turn(name, turn_id, expected=claim)


def _waiting(tmp_path, *, second=False):
    comms = wire(tmp_path / "wire")
    comms._test_claims = {}
    _thread(comms, "owner", tmp_path)
    _thread(comms, "child", tmp_path)
    _begin(comms, "child", "child-turn")
    if second:
        _thread(comms, "other", tmp_path)
        _begin(comms, "other", "other-turn")
    goal = comms.update_goal("owner", "set", text="Review the delegated work")
    assert goal is not None
    targets = ["child", "other"] if second else ["child"]
    comms.update_goal("owner", "standby", goal_id=goal.id, wait_for=targets)
    return comms, goal


def test_idle_target_refusal_is_side_effect_free(tmp_path):
    comms = wire(tmp_path / "wire")
    _thread(comms, "owner", tmp_path)
    _thread(comms, "child", tmp_path)
    goal = comms.update_goal("owner", "set", text="Work")
    assert goal is not None
    authoritative = ("registry.json", "goal_waits.json", "input_dispositions.json")
    before = {
        name: (comms.root / name).read_bytes() if (comms.root / name).exists() else None
        for name in authoritative
    }
    with pytest.raises(ValueError, match="No declared dependency has an active turn.*@child"):
        comms.update_goal("owner", "standby", goal_id=goal.id, wait_for=["child"])
    assert {
        name: (comms.root / name).read_bytes() if (comms.root / name).exists() else None
        for name in authoritative
    } == before
    assert comms.registry.require("owner").goal == goal
    assert comms.goal_wait("owner") is None


def test_optional_reply_read_failure_after_terminal_commit_never_releases_or_fails(
    tmp_path, monkeypatch
):
    comms, goal = _waiting(tmp_path)
    fence = _finish(comms, "child", "child-turn")
    assert fence is not None and comms.registry.require("child").active_turn is None
    original = comms.bus._history_page

    def unavailable(*_args, **_kwargs):
        raise OSError("injected optional direct-reply read failure")

    monkeypatch.setattr(comms.bus, "_history_page", unavailable)
    assert comms.release_waits_after_terminal_turn(fence) == ()
    assert comms.registry.require("child").active_turn is None
    assert comms.registry.require("owner").goal.active
    assert comms.registry.require("owner").goal.id == goal.id
    assert comms.goal_wait("owner") is not None
    monkeypatch.setattr(comms.bus, "_history_page", original)
    assert comms.release_waits_after_terminal_turn(fence) == ("owner",)


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
        comms.release_waits_after_terminal_turn(fence)
    assert comms.registry.require("owner").goal.active
    assert comms.goal_wait("owner") is not None
    monkeypatch.setattr(comms.registry, "register", original_register)
    assert comms.release_waits_after_terminal_turn(fence) == ("owner",)


def test_mixed_targets_project_idle_without_suppressing_active_alternative(tmp_path):
    comms, goal = _waiting(tmp_path, second=True)
    other_fence = _finish(comms, "other", "other-turn")
    execution = comms.goal_execution("owner")
    assert [target.name for target in execution.inactive_wait_for] == ["other"]
    assert "no active turn: @other" in execution.presentation("owner").summary
    assert comms.release_waits_after_terminal_turn(other_fence) == ()
    assert comms.registry.require("owner").goal.active
    assert comms.goal_wait("owner") is not None
    assert goal.id == comms.registry.require("owner").goal.id


def test_last_target_finishes_silently_releases_once_and_reopens(tmp_path):
    comms, goal = _waiting(tmp_path, second=True)
    other_fence = _finish(comms, "other", "other-turn")
    assert comms.release_waits_after_terminal_turn(other_fence) == ()
    child_fence = _finish(comms, "child", "child-turn")
    assert comms.release_waits_after_terminal_turn(child_fence) == ("owner",)
    reopened = Comms(comms.root)
    continued = reopened.registry.require("owner").goal
    assert continued.id == goal.id and continued.status == "active"
    assert continued.revision == goal.revision + 2
    assert "without a qualifying direct reply" in continued.progress
    assert reopened.goal_execution("owner").state.value == "runnable"
    assert reopened.goal_wait("owner") is None
    assert reopened.goal_pause("owner") is None
    assert reopened.release_waits_after_terminal_turn(child_fence) == ()
    assert reopened.registry.require("owner").goal == continued
    assert not (comms.root / "goal-private").exists()


@pytest.mark.parametrize("notice", [False, True])
def test_only_substantive_direct_reply_prevents_release(tmp_path, notice):
    comms, _goal = _waiting(tmp_path)
    comms.send_message(
        "child", "owner", "Result" if not notice else "Failure notice", notice=notice
    )
    child_fence = _finish(comms, "child", "child-turn")
    changed = comms.release_waits_after_terminal_turn(child_fence)
    assert changed == (("owner",) if notice else ())
    assert comms.registry.require("owner").goal.status == "active"
    assert (comms.goal_wait("owner") is None) is notice


def test_renamed_target_keeps_incarnation_but_replacement_fails_closed(tmp_path):
    comms, goal = _waiting(tmp_path)
    comms.registry.rename("child", "renamed-child")
    child_fence = _finish(comms, "renamed-child", "child-turn")
    assert comms.release_waits_after_terminal_turn(child_fence) == ("owner",)
    assert comms.registry.require("owner").goal.status == "active"
    assert comms.registry.require("owner").goal.id == goal.id
    # A later registered incarnation must not report an older turn's result.
    assert (
        comms.release_waits_after_terminal_turn(
            replace(child_fence, created_at=child_fence.created_at + 1)
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
        comms.release_waits_after_terminal_turn(child_fence)
    reopened = Comms(comms.root)
    assert reopened.registry.require("owner").goal.status == "active"
    assert reopened.registry.require("owner").goal.id == goal.id
    assert reopened.goal_execution("owner").state.value == "standby"
    assert reopened.goal_wait("owner") is not None
    assert reopened.registry.status("owner") is ThreadStatus.RUNNING
    monkeypatch.setattr(GoalWaits, "clear", original_clear)
    assert reopened.recover_closed_goal_wait("owner") == ("owner",)
    assert reopened.goal_execution("owner").state.value == "runnable"


def test_replaced_target_cannot_report_for_old_incarnation(tmp_path):
    comms, goal = _waiting(tmp_path)
    old = comms.registry.require("child")
    old_fence = _finish(comms, "child", "child-turn")
    comms.registry.unregister("child")
    comms.registry.remove("child")
    _thread(comms, "child", tmp_path)
    replacement = comms.registry.require("child")
    assert replacement.created_at != old.created_at
    assert comms.release_waits_after_terminal_turn(old_fence) == ()
    execution = comms.goal_execution("owner")
    assert [target.name for target in execution.inactive_wait_for] == ["child"]
    assert comms.registry.require("owner").goal.id == goal.id
    # Removal/replacement has no subscribed terminal hook in this prototype.
    assert comms.registry.require("owner").goal.active


def test_owner_pause_still_prevents_quiet_dependency_release(tmp_path):
    comms, goal = _waiting(tmp_path)
    paused = comms.update_goal("owner", "paused", goal_id=goal.id, owner_action=True)
    assert paused is not None
    fence = _finish(comms, "child", "child-turn")
    assert comms.release_waits_after_terminal_turn(fence) == ()
    assert comms.registry.require("owner").goal == paused
    assert comms.goal_pause("owner").source == "owner"
    assert comms.goal_wait("owner") is None


def test_reply_arriving_after_idle_check_stays_visible_without_model_start(tmp_path, monkeypatch):
    comms, _goal = _waiting(tmp_path)
    child_fence = _finish(comms, "child", "child-turn")
    checked = threading.Event()
    sent = threading.Event()

    def late_send():
        checked.set()
        comms.send_message("child", "owner", "Late direct reply")
        sent.set()

    real_history = comms.bus._history_page
    sender = threading.Thread(target=late_send)

    def interpose(*args, **kwargs):
        sender.start()
        assert checked.wait(timeout=2)
        return real_history(*args, **kwargs)

    monkeypatch.setattr(comms.bus, "_history_page", interpose)
    assert comms.release_waits_after_terminal_turn(child_fence) == ("owner",)
    sender.join(timeout=2)
    assert sent.is_set()
    assert comms.registry.require("owner").goal.status == "active"
    assert [message.body for message in comms.inbox("owner")] == ["Late direct reply"]
    assert comms.goal_wait("owner") is None
    assert not (comms.root / "goal-private").exists()


def test_terminal_hook_rechecks_goal_and_owner_identity(tmp_path):
    comms, goal = _waiting(tmp_path)
    child_fence = _finish(comms, "child", "child-turn")
    newer = comms.update_goal("owner", "edit", goal_id=goal.id, text="new exact objective")
    assert newer is not None
    # Goal text edits preserve wait; the exact current revision is updated, not lost.
    assert comms.release_waits_after_terminal_turn(child_fence) == ("owner",)
    assert comms.registry.require("owner").goal.revision == newer.revision + 1
    assert comms.registry.require("owner").goal.text == newer.text
    assert comms.release_waits_after_terminal_turn(child_fence) == ()


def test_stopped_dependency_cannot_be_declared_live(tmp_path):
    comms = wire(tmp_path / "wire")
    _thread(comms, "owner", tmp_path)
    _thread(comms, "child", tmp_path)
    comms.begin_turn("child", "work")
    child = comms.registry.require("child")
    comms.registry.register(replace(child, active_turn=None), ThreadStatus.STOPPED)
    goal = comms.update_goal("owner", "set", text="Review")
    assert goal is not None
    with pytest.raises(ValueError, match="No declared dependency has an active turn"):
        comms.update_goal("owner", "standby", goal_id=goal.id, wait_for=["child"])


def test_delayed_old_finish_cannot_release_new_active_same_id(tmp_path):
    comms, _goal = _waiting(tmp_path)
    old_claim = comms._test_claims["child"]
    old_fence = _finish(comms, "child", "child-turn")
    new_claim = _begin(comms, "child", "child-turn")
    assert new_claim.turn_generation == old_claim.turn_generation + 1
    active = comms.registry.require("child").active_turn
    assert comms.finish_turn("child", "child-turn", expected=old_claim) is None
    assert comms.registry.require("child").active_turn == active
    assert comms.release_waits_after_terminal_turn(old_fence) == ()
    assert comms.registry.require("owner").goal.active
    assert comms.goal_wait("owner") is not None
    new_fence = comms.finish_turn("child", "child-turn", expected=new_claim)
    assert new_fence is not None and new_fence.turn_generation == new_claim.turn_generation
    assert comms.release_waits_after_terminal_turn(new_fence) == ("owner",)


def test_legacy_id_only_finish_cannot_attest_release(tmp_path):
    comms, _goal = _waiting(tmp_path)
    assert comms.finish_turn("child", "child-turn") is None
    assert comms.registry.require("child").active_turn is None
    assert comms.release_waits_after_terminal_turn(None) == ()
    assert comms.registry.require("owner").goal.active


@pytest.mark.parametrize("reuse_turn_id", [False, True])
def test_delayed_old_terminal_cannot_release_newer_turn_before_reply(tmp_path, reuse_turn_id):
    comms, goal = _waiting(tmp_path)
    old_fence = _finish(comms, "child", "child-turn")
    assert old_fence is not None
    newer_id = "child-turn" if reuse_turn_id else "new-child-turn"
    _begin(comms, "child", newer_id)
    new_fence = _finish(comms, "child", newer_id)
    assert new_fence is not None
    assert new_fence.turn_generation == old_fence.turn_generation + 1
    assert comms.release_waits_after_terminal_turn(old_fence) == ()
    assert comms.registry.require("owner").goal.active
    assert comms.goal_wait("owner") is not None
    # The newer turn publishes after the OLD callback, before its own callback.
    comms.send_message("child", "owner", "New turn's substantive answer")
    assert comms.release_waits_after_terminal_turn(new_fence) == ()
    assert comms.registry.require("owner").goal.active
    assert comms.registry.require("owner").goal.id == goal.id
    assert comms.goal_wait("owner") is not None
    assert not (comms.root / "goal-private").exists()


def test_newer_silent_terminal_releases_waiter(tmp_path):
    comms, _goal = _waiting(tmp_path)
    old_fence = _finish(comms, "child", "child-turn")
    _begin(comms, "child", "new-child-turn")
    new_fence = _finish(comms, "child", "new-child-turn")
    assert comms.release_waits_after_terminal_turn(old_fence) == ()
    assert comms.release_waits_after_terminal_turn(new_fence) == ("owner",)
    assert comms.goal_wait("owner") is None


def test_terminal_fence_survives_rename_not_stop_or_metadata_edit(tmp_path):
    comms, _goal = _waiting(tmp_path)
    fence = _finish(comms, "child", "child-turn")
    assert fence is not None
    child = comms.registry.require("child")
    comms.registry.register(replace(child, title="New title"), ThreadStatus.RUNNING)
    comms.registry.rename("child", "renamed-child")
    assert comms.release_waits_after_terminal_turn(replace(fence, turn_generation=2)) == ()
    assert comms.release_waits_after_terminal_turn(fence) == ("owner",)

    another, _goal = _waiting(tmp_path / "stop")
    stale = _finish(another, "child", "child-turn")
    child = another.registry.require("child")
    another.registry.unregister("child")
    another.registry.register(replace(child, active_turn=None), ThreadStatus.RUNNING)
    assert another.release_waits_after_terminal_turn(stale) == ()
    assert another.registry.require("owner").goal.active


def test_legacy_wait_without_turn_generation_cannot_infer_terminal_authority(tmp_path):
    comms, _goal = _waiting(tmp_path)
    wait = comms.goal_wait("owner")
    assert wait is not None
    GoalWaits(comms.root / "goal_waits.json").record(replace(wait, target_turn_generations=()))
    fence = _finish(comms, "child", "child-turn")
    assert comms.release_waits_after_terminal_turn(fence) == ()
    assert comms.registry.require("owner").goal.active


async def test_acp_optional_reply_read_failure_after_settled_does_not_fail_done(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True, auto_wake=False)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    child = (await agent.new_session(str(tmp_path / "child"))).session_id
    _thread(comms, "owner", tmp_path)
    goal = comms.update_goal("owner", "set", text="Await child")
    assert goal is not None
    terminal = []
    original_emit = agent._emit_event

    async def capture_emit(session_id, event):
        if event.get("type") == "settled":
            terminal.append("settled")
        await original_emit(session_id, event)

    async def events(*_args, **_kwargs):
        comms.update_goal("owner", "standby", goal_id=goal.id, wait_for=[child])
        yield {"type": "settled"}
        yield {"type": "done", "ok": True, "text": ""}

    original_history_page = comms.bus._history_page

    def unavailable(*args, **kwargs):
        if not terminal:
            return original_history_page(*args, **kwargs)
        assert terminal == ["settled"]
        assert comms.registry.require(child).active_turn is None
        raise OSError("injected optional direct-reply read failure")

    monkeypatch.setattr(agent, "_emit_event", capture_emit)
    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    monkeypatch.setattr(comms.bus, "_history_page", unavailable)
    try:
        await agent._run_agent_turn(child, child, "Finish work")
        assert terminal == ["settled"]
        assert comms.registry.require("owner").goal.active
        assert comms.goal_wait("owner") is not None
        assert comms.registry.require(child).active_turn is None
    finally:
        await agent.shutdown()


async def test_quiet_dependency_finish_schedules_the_still_active_goal(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True, auto_wake=False)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    owner = (await agent.new_session(str(tmp_path / "owner"))).session_id
    child = (await agent.new_session(str(tmp_path / "child"))).session_id
    goal = comms.update_goal(
        owner, "set", text="Review child work", owner_store=agent._open_goal_store()
    )
    assert goal is not None
    wakes = []
    monkeypatch.setattr(agent, "_schedule_wake", wakes.append)
    try:
        claim = comms.begin_turn(child, "child-turn")
        comms.update_goal(owner, "standby", goal_id=goal.id, wait_for=[child])
        assert not agent._pending_turns.get(owner)
        fence = comms.finish_turn(child, "child-turn", expected=claim)
        assert comms.release_waits_after_terminal_turn(fence) == (owner,)
        assert comms.registry.require(owner).goal.active
        assert comms.goal_wait(owner) is None
        agent._schedule_goal(owner)
        assert wakes == [owner]
        assert [turn.goal_id for turn in agent._pending_turns[owner]] == [goal.id]
    finally:
        await agent.shutdown()


async def test_acp_delayed_old_callback_after_new_finish_before_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True, auto_wake=False)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    child = (await agent.new_session(str(tmp_path / "child"))).session_id
    _thread(comms, "owner", tmp_path)
    goal = comms.update_goal("owner", "set", text="Await child")
    assert goal is not None
    old_settled = asyncio.Event()
    release_old = asyncio.Event()
    real_emit = agent._emit_event

    async def delayed_emit(session_id, event):
        if event.get("type") == "settled" and not old_settled.is_set():
            old_settled.set()
            await release_old.wait()
        await real_emit(session_id, event)

    async def events(*_args, **_kwargs):
        comms.update_goal("owner", "standby", goal_id=goal.id, wait_for=[child])
        yield {"type": "settled"}
        yield {"type": "done", "ok": True, "text": ""}

    monkeypatch.setattr(agent, "_emit_event", delayed_emit)
    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    old_task = asyncio.create_task(agent._run_agent_turn(child, child, "Finish work"))
    try:
        await asyncio.wait_for(old_settled.wait(), 2)
        assert comms.registry.require(child).active_turn is None
        new_claim = comms.begin_turn(child, "new-child-turn")
        new_fence = comms.finish_turn(child, "new-child-turn", expected=new_claim)
        release_old.set()
        await asyncio.wait_for(old_task, 2)  # OLD callback sees idle NEW turn, no reply yet.
        assert comms.registry.require("owner").goal.active
        assert comms.goal_wait("owner") is not None
        comms.send_message(child, "owner", "New turn's result")
        assert comms.release_waits_after_terminal_turn(new_fence) == ()
        assert comms.registry.require("owner").goal.active
    finally:
        release_old.set()
        if not old_task.done():
            await asyncio.wait_for(old_task, 2)
        await agent.shutdown()


@pytest.mark.parametrize("reply", [False, True])
async def test_acp_settled_is_not_terminal_reply_and_never_admits_waiter_model(
    tmp_path, monkeypatch, reply
):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True, auto_wake=False)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    child = (await agent.new_session(str(tmp_path / "child"))).session_id
    _thread(comms, "owner", tmp_path)
    goal = comms.update_goal("owner", "set", text="Await child")
    assert goal is not None
    streamed = []

    async def events(*_args, **_kwargs):
        streamed.append(child)
        # The active child turn began before model events are streamed.
        comms.update_goal("owner", "standby", goal_id=goal.id, wait_for=[child])
        if reply:
            yield {"type": "chunk", "text": "Reported"}
        yield {"type": "settled"}
        assert comms.registry.require("owner").goal.active
        assert comms.goal_wait("owner") is not None
        yield {"type": "done", "ok": True, "text": "Reported" if reply else ""}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent._run_agent_turn(
            child, child, "Finish work", reply_targets=("owner",) if reply else ()
        )
        assert streamed == [child]
        assert not agent._pending_turns.get("owner")
        assert not (comms.root / "goal-private").exists()
        if reply:
            assert comms.registry.require("owner").goal.active
            assert comms.goal_wait("owner") is not None
            assert any(m.body == "Reported" for m in comms.inbox("owner"))
        else:
            assert comms.registry.require("owner").goal.status == "active"
            assert comms.goal_wait("owner") is None
    finally:
        await agent.shutdown()
