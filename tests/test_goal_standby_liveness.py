"""Standby liveness preserves goal authority and never replays an input."""
from agent_comms.field_codec import TurnId


from agent_comms.messages import ConversationRole, NoticeRole
import os
from dataclasses import replace

import pytest

from agent_comms.turn_lease import NoFinishedTurn

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.goal_actions import (
    EditGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.goal_waits import GoalWaits
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
from goal_owner_fixture import canonical_goal_wire
from agent_comms.goal_waits import NoGoalWait
from agent_comms.goal_waits import RecordedGoalWait
from agent_comms.thread_text import GivenThreadText
from agent_comms.threads import Thread



def _thread(comms, name, worktree):
    comms.registry.declare(
        Thread(name, frozenset(), str(worktree), process_identity=ProcessIdentity.capture(os.getpid()))
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
    _begin(comms, "child", TurnId('9782df8c75cffb6a483d963ccaefc23b'))
    if second:
        _thread(comms, "other", tmp_path)
        _begin(comms, "other", TurnId('e2141dc9c71046570fc0e6a19ad6f5c1'))
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Review the delegated work"))
    assert goal is not None
    targets = ["child", "other"] if second else ["child"]
    comms.goals.update_goal(
        "owner", StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=targets)
    )
    return comms, goal


def test_stopped_target_refusal_is_side_effect_free(tmp_path):
    comms = canonical_goal_wire(tmp_path / "wire")
    _thread(comms, "owner", tmp_path)
    _thread(comms, "child", tmp_path)
    comms.registry.register(comms.registry.require("child"), StoppedThreadStatus())
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Work"))
    assert goal is not None
    authoritative = ("registry.json", "goal_waits.sqlite3", "input_dispositions.sqlite3")
    before = {
        name: (comms.root / name).read_bytes() if (comms.root / name).exists() else None
        for name in authoritative
    }
    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "owner",
            StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("child",)),
        )
    assert {
        name: (comms.root / name).read_bytes() if (comms.root / name).exists() else None
        for name in authoritative
    } == before
    assert comms.registry.require("owner").goal == goal
    assert isinstance(comms.goals.goal_wait("owner"), NoGoalWait)


def test_mixed_targets_project_idle_without_suppressing_active_alternative(tmp_path):
    comms, goal = _waiting(tmp_path, second=True)
    other_fence = _finish(comms, "other", TurnId('e2141dc9c71046570fc0e6a19ad6f5c1'))
    execution = comms.goals.goal_execution("owner")
    assert [target.incarnation.name for target in execution.inactive_wait_for] == ["other"]
    assert "no active turn: @other" in execution.presentation("owner").summary
    assert comms.goals.release_waits_after_terminal_turn(other_fence) == ()
    comms.registry.require('owner').goal.require_active()
    assert isinstance(comms.goals.goal_wait("owner"), RecordedGoalWait)
    assert goal.id == comms.registry.require("owner").goal.id


def test_last_target_finishes_silently_releases_once_and_reopens(tmp_path):
    comms, goal = _waiting(tmp_path, second=True)
    other_fence = _finish(comms, "other", TurnId('e2141dc9c71046570fc0e6a19ad6f5c1'))
    assert comms.goals.release_waits_after_terminal_turn(other_fence) == ()
    child_fence = _finish(comms, "child", TurnId('9782df8c75cffb6a483d963ccaefc23b'))
    assert comms.goals.release_waits_after_terminal_turn(child_fence) == ("owner",)
    reopened = wire(comms.root)
    continued = reopened.registry.require("owner").goal
    assert continued.id == goal.id and continued.state.declared_name == "active"
    assert continued.revision == goal.revision + 2
    assert "without a qualifying direct reply" in continued.progress
    assert reopened.goals.goal_execution("owner").runnable()
    assert isinstance(reopened.goals.goal_wait("owner"), NoGoalWait)
    assert reopened.registry.require("owner").goal.state.pause_source is None
    assert reopened.goals.release_waits_after_terminal_turn(child_fence) == ()
    assert reopened.registry.require("owner").goal == continued


@pytest.mark.parametrize(
    "role,body,expected_changed,released",
    [
        (ConversationRole(), "Result", (), False),
        (NoticeRole(), "Failure notice", ("owner",), True),
    ],
)
def test_only_substantive_direct_reply_prevents_release(
    tmp_path, role, body, expected_changed, released
):
    comms, _goal = _waiting(tmp_path)
    comms.messaging.send("child", "owner", body, role=role).message
    child_fence = _finish(comms, "child", TurnId('9782df8c75cffb6a483d963ccaefc23b'))
    changed = comms.goals.release_waits_after_terminal_turn(child_fence)
    assert changed == expected_changed
    assert comms.registry.require("owner").goal.state.declared_name == "active"
    assert isinstance(comms.goals.goal_wait("owner"), NoGoalWait) is released


def test_renamed_target_keeps_incarnation_but_replacement_fails_closed(tmp_path):
    comms, goal = _waiting(tmp_path)
    comms.registry.rename("child", "renamed-child")
    child_fence = _finish(comms, "renamed-child", TurnId('9782df8c75cffb6a483d963ccaefc23b'))
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


def test_replaced_target_cannot_report_for_old_incarnation(tmp_path):
    comms, goal = _waiting(tmp_path)
    old = comms.registry.require("child")
    old_fence = _finish(comms, "child", TurnId('9782df8c75cffb6a483d963ccaefc23b'))
    comms.registry.unregister("child")
    comms.registry.remove("child")
    _thread(comms, "child", tmp_path)
    replacement = comms.registry.require("child")
    assert replacement.created_at != old.created_at
    assert comms.goals.release_waits_after_terminal_turn(old_fence) == ()
    execution = comms.goals.goal_execution("owner")
    assert [target.incarnation.name for target in execution.inactive_wait_for] == ["child"]
    assert comms.registry.require("owner").goal.id == goal.id
    # Removal/replacement has no subscribed terminal hook in this prototype.
    comms.registry.require('owner').goal.require_active()


def test_owner_pause_still_prevents_quiet_dependency_release(tmp_path):
    comms, goal = _waiting(tmp_path)
    paused = comms.goals.update_goal(
        "owner", PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)), actor=OwnerInvocable
    )
    assert paused is not None
    fence = _finish(comms, "child", TurnId('9782df8c75cffb6a483d963ccaefc23b'))
    assert comms.goals.release_waits_after_terminal_turn(fence) == ()
    assert comms.registry.require("owner").goal == paused
    assert comms.registry.require("owner").goal.state.pause_source.declared_name == "owner"
    assert isinstance(comms.goals.goal_wait("owner"), NoGoalWait)


def test_terminal_hook_rechecks_goal_and_owner_identity(tmp_path):
    comms, goal = _waiting(tmp_path)
    child_fence = _finish(comms, "child", TurnId('9782df8c75cffb6a483d963ccaefc23b'))
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
    comms.agents.begin_turn("child", TurnId('00e13ed7af55b27622f1d6eab5bec014'))
    child = comms.registry.require("child")
    comms.registry.register(child.without_turn(), StoppedThreadStatus())
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Review"))
    assert goal is not None
    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "owner",
            StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("child",)),
        )


def test_delayed_old_finish_cannot_release_new_active_same_id(tmp_path):
    comms, _goal = _waiting(tmp_path)
    old_claim = comms._test_leases["child"]
    old_fence = _finish(comms, "child", TurnId('9782df8c75cffb6a483d963ccaefc23b'))
    new_claim = _begin(comms, "child", TurnId('9782df8c75cffb6a483d963ccaefc23b'))
    assert new_claim.identity.generation == old_claim.identity.generation + 1
    active = comms.registry.require("child").turn_record.read()
    assert comms.agents.finish_turn(old_claim) == NoFinishedTurn()
    assert comms.registry.require("child").turn_record.read() == active
    assert comms.goals.release_waits_after_terminal_turn(old_fence) == ()
    comms.registry.require('owner').goal.require_active()
    assert isinstance(comms.goals.goal_wait("owner"), RecordedGoalWait)
    new_fence = comms.agents.finish_turn(new_claim)
    assert new_fence is not None and new_fence.identity.generation == new_claim.identity.generation
    assert comms.goals.release_waits_after_terminal_turn(new_fence) == ("owner",)


@pytest.mark.parametrize("reuse_turn_id", [False, True])
def test_delayed_old_terminal_cannot_release_newer_turn_before_reply(tmp_path, reuse_turn_id):
    comms, goal = _waiting(tmp_path)
    old_fence = _finish(comms, "child", TurnId('9782df8c75cffb6a483d963ccaefc23b'))
    assert old_fence is not None
    newer_id = old_fence.turn_id if reuse_turn_id else TurnId.mint()
    _begin(comms, "child", newer_id)
    new_fence = _finish(comms, "child", newer_id)
    assert new_fence is not None
    assert new_fence.identity.generation == old_fence.identity.generation + 1
    assert comms.goals.release_waits_after_terminal_turn(old_fence) == ()
    comms.registry.require('owner').goal.require_active()
    assert isinstance(comms.goals.goal_wait("owner"), RecordedGoalWait)
    # The newer turn publishes after the OLD callback, before its own callback.
    comms.messaging.send("child", "owner", "New turn's substantive answer").message
    assert comms.goals.release_waits_after_terminal_turn(new_fence) == ()
    comms.registry.require('owner').goal.require_active()
    assert comms.registry.require("owner").goal.id == goal.id
    assert isinstance(comms.goals.goal_wait("owner"), RecordedGoalWait)


def test_newer_silent_terminal_releases_waiter(tmp_path):
    comms, _goal = _waiting(tmp_path)
    old_fence = _finish(comms, "child", TurnId('9782df8c75cffb6a483d963ccaefc23b'))
    _begin(comms, "child", TurnId('25b63a30f24cf9d67463f931d800c73a'))
    new_fence = _finish(comms, "child", TurnId('25b63a30f24cf9d67463f931d800c73a'))
    assert comms.goals.release_waits_after_terminal_turn(old_fence) == ()
    assert comms.goals.release_waits_after_terminal_turn(new_fence) == ("owner",)
    assert isinstance(comms.goals.goal_wait("owner"), NoGoalWait)


def test_terminal_fence_survives_rename_not_stop_or_metadata_edit(tmp_path):
    comms, _goal = _waiting(tmp_path)
    fence = _finish(comms, "child", TurnId('9782df8c75cffb6a483d963ccaefc23b'))
    assert fence is not None
    child = comms.registry.require("child")
    comms.registry.register(replace(child, title=GivenThreadText("New title")), RunningThreadStatus())
    comms.registry.rename("child", "renamed-child")
    assert (
        comms.goals.release_waits_after_terminal_turn(
            replace(fence, identity=replace(fence.identity, generation=2))
        )
        == ()
    )
    assert comms.goals.release_waits_after_terminal_turn(fence) == ("owner",)

    another, _goal = _waiting(tmp_path / "stop")
    stale = _finish(another, "child", TurnId('9782df8c75cffb6a483d963ccaefc23b'))
    child = another.registry.require("child")
    another.registry.unregister("child")
    another.registry.register(child.without_turn(), RunningThreadStatus())
    assert another.goals.release_waits_after_terminal_turn(stale) == ()
    another.registry.require('owner').goal.require_active()


def test_unbound_wait_is_rejected_without_replacing_current_wait(tmp_path):
    comms, _goal = _waiting(tmp_path)
    wait = comms.goals.goal_wait("owner")
    before = GoalWaits(comms.root / "goal_waits.sqlite3").path.read_bytes()
    with pytest.raises(ValueError):
        GoalWaits(comms.root / "goal_waits.sqlite3").record(replace(wait, target_turn_generations=()))
    assert GoalWaits(comms.root / "goal_waits.sqlite3").path.read_bytes() == before


