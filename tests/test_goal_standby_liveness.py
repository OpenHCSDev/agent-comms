"""Standby liveness is advisory status, never a model or N/K admission."""

import os
import threading
from dataclasses import replace

import pytest

from agent_comms import Thread, wire
from agent_comms.acp import CommsAgent
from agent_comms.declarations import GoalPauseSource, ThreadStatus
from agent_comms.goal_waits import GoalWaits
from agent_comms.operations import Comms


def _thread(comms, name, worktree):
    comms.register(Thread(name, frozenset(), str(worktree), pid=os.getpid()))


def _waiting(tmp_path, *, second=False):
    comms = wire(tmp_path / "wire")
    _thread(comms, "owner", tmp_path)
    _thread(comms, "child", tmp_path)
    comms.begin_turn("child", "child-turn")
    if second:
        _thread(comms, "other", tmp_path)
        comms.begin_turn("other", "other-turn")
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


def test_mixed_targets_project_idle_without_suppressing_active_alternative(tmp_path):
    comms, goal = _waiting(tmp_path, second=True)
    comms.finish_turn("other", "other-turn")
    execution = comms.goal_execution("owner")
    assert [target.name for target in execution.inactive_wait_for] == ["other"]
    assert "no active turn: @other" in execution.presentation("owner").summary
    assert (
        comms.pause_waits_after_terminal_turn(
            "other", created_at=comms.registry.require("other").created_at
        )
        == ()
    )
    assert comms.registry.require("owner").goal.active
    assert comms.goal_wait("owner") is not None
    assert goal.id == comms.registry.require("owner").goal.id


def test_last_target_finishes_silently_pauses_once_and_reopens(tmp_path):
    comms, goal = _waiting(tmp_path, second=True)
    comms.finish_turn("other", "other-turn")
    other = comms.registry.require("other")
    assert comms.pause_waits_after_terminal_turn("other", created_at=other.created_at) == ()
    comms.finish_turn("child", "child-turn")
    child = comms.registry.require("child")
    assert comms.pause_waits_after_terminal_turn("child", created_at=child.created_at) == ("owner",)
    reopened = Comms(comms.root)
    paused = reopened.registry.require("owner").goal
    assert paused.id == goal.id and paused.status == "paused"
    assert paused.revision == goal.revision + 2
    assert "without a qualifying direct reply" in paused.progress
    assert reopened.goal_execution("owner").state.value == "paused"
    assert reopened.goal_wait("owner") is None
    assert reopened.goal_pause("owner").source is GoalPauseSource.RUNTIME
    assert reopened.pause_waits_after_terminal_turn("child", created_at=child.created_at) == ()
    assert reopened.registry.require("owner").goal == paused
    assert not (comms.root / "goal-private").exists()


@pytest.mark.parametrize("notice", [False, True])
def test_only_substantive_direct_reply_prevents_pause(tmp_path, notice):
    comms, _goal = _waiting(tmp_path)
    child = comms.registry.require("child")
    comms.send_message(
        "child", "owner", "Result" if not notice else "Failure notice", notice=notice
    )
    comms.finish_turn("child", "child-turn")
    changed = comms.pause_waits_after_terminal_turn("child", created_at=child.created_at)
    assert changed == (("owner",) if notice else ())
    assert (comms.registry.require("owner").goal.status == "paused") is notice
    assert (comms.goal_wait("owner") is None) is notice


def test_renamed_target_keeps_incarnation_but_replacement_fails_closed(tmp_path):
    comms, goal = _waiting(tmp_path)
    child = comms.registry.require("child")
    comms.registry.rename("child", "renamed-child")
    comms.finish_turn("renamed-child", "child-turn")
    assert comms.pause_waits_after_terminal_turn("child", created_at=child.created_at) == ("owner",)
    assert comms.registry.require("owner").goal.status == "paused"
    assert comms.registry.require("owner").goal.id == goal.id
    # A later registered incarnation must not report an older turn's result.
    assert (
        comms.pause_waits_after_terminal_turn("renamed-child", created_at=child.created_at + 1)
        == ()
    )


def test_pause_write_precedes_wait_clear_on_crash_and_stays_nonrunnable(tmp_path, monkeypatch):
    comms, goal = _waiting(tmp_path)
    child = comms.registry.require("child")
    comms.finish_turn("child", "child-turn")

    def crash_before_wait_clear(*_args, **_kwargs):
        raise OSError("injected crash after paused registry")

    monkeypatch.setattr(GoalWaits, "clear", crash_before_wait_clear)
    with pytest.raises(OSError, match="injected crash"):
        comms.pause_waits_after_terminal_turn("child", created_at=child.created_at)
    reopened = Comms(comms.root)
    assert reopened.registry.require("owner").goal.status == "paused"
    assert reopened.registry.require("owner").goal.id == goal.id
    assert reopened.goal_execution("owner").state.value == "paused"
    assert reopened.goal_wait("owner") is None  # orphan wait is not launch authority
    assert reopened.registry.status("owner") is ThreadStatus.RUNNING


def test_replaced_target_cannot_report_for_old_incarnation(tmp_path):
    comms, goal = _waiting(tmp_path)
    old = comms.registry.require("child")
    comms.registry.unregister("child")
    comms.registry.remove("child")
    _thread(comms, "child", tmp_path)
    replacement = comms.registry.require("child")
    assert replacement.created_at != old.created_at
    assert comms.pause_waits_after_terminal_turn("child", created_at=old.created_at) == ()
    execution = comms.goal_execution("owner")
    assert [target.name for target in execution.inactive_wait_for] == ["child"]
    assert comms.registry.require("owner").goal.id == goal.id
    # Removal/replacement has no subscribed terminal hook in this prototype.
    assert comms.registry.require("owner").goal.active


def test_reply_arriving_after_idle_check_stays_visible_without_model_start(tmp_path, monkeypatch):
    comms, _goal = _waiting(tmp_path)
    child = comms.registry.require("child")
    comms.finish_turn("child", "child-turn")
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
    assert comms.pause_waits_after_terminal_turn("child", created_at=child.created_at) == ("owner",)
    sender.join(timeout=2)
    assert sent.is_set()
    assert comms.registry.require("owner").goal.status == "paused"
    assert [message.body for message in comms.inbox("owner")] == ["Late direct reply"]
    assert comms.goal_wait("owner") is None
    assert not (comms.root / "goal-private").exists()


def test_terminal_hook_rechecks_goal_and_owner_identity(tmp_path):
    comms, goal = _waiting(tmp_path)
    child = comms.registry.require("child")
    comms.finish_turn("child", "child-turn")
    newer = comms.update_goal("owner", "edit", goal_id=goal.id, text="new exact objective")
    assert newer is not None
    # Goal text edits preserve wait; the exact current revision is updated, not lost.
    assert comms.pause_waits_after_terminal_turn("child", created_at=child.created_at) == ("owner",)
    assert comms.registry.require("owner").goal.revision == newer.revision + 1
    assert comms.registry.require("owner").goal.text == newer.text
    assert comms.pause_waits_after_terminal_turn("child", created_at=child.created_at) == ()


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
            assert comms.registry.require("owner").goal.status == "paused"
            assert comms.goal_wait("owner") is None
    finally:
        await agent.shutdown()
