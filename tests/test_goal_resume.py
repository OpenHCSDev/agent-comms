"""Same-ID, explicitly requested goal resume through the public tool boundary."""


import os

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.lifecycle_capability import Active
from agent_comms.field_codec import FieldCodec, TurnId, GoalId
from agent_comms.goal_actions import (
    ActiveGoalAction,
    BlockedGoalAction,
    ClearGoalAction,
    CompletedGoalAction,
    GoalPrecondition,
    PausedGoalAction,
    RetryGoalAction,
    SetGoalAction,
)
from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.goal_generation import BlockedGeneration, CancelledGeneration, CompletedGeneration
from agent_comms.goals import Goal, NoGoal
from agent_comms.reported_turn import ReportedTurn
from agent_comms.tools import invoke_tool
from agent_comms.tool_caller import ThreadCaller
from functools import partial
from agent_comms.threads import Thread
from agent_comms.goal_scope import OneGoal

# Every case in this module exercises the explicitly declared owner.
invoke_tool = partial(invoke_tool, caller=ThreadCaller("owner"))


def _goal(comms, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_THREAD", "owner")
    comms.registry.declare(Thread(name='owner', tags=frozenset(), worktree='/wt'))
    return invoke_tool(comms, "comms_set_goal", {"text": "keep working"})["goal"]


def test_same_id_resume_from_paused(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    started_goal_id = FieldCodec.decode(GoalId, started["id"])
    comms.goals.update_goal(
        "owner",
        PausedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(started_goal_id)), progress="previous"),
    )

    result = invoke_tool(
        comms, "comms_resume_goal", {"goal_id": started["id"], "progress": "user resumed"}
    )["goal"]

    assert result["id"] == started["id"]
    assert result["text"] == started["text"]
    FieldCodec.decode(Goal, result).require_active()
    assert result["progress"] == "user resumed"
    assert comms.registry.require("owner").goal.id == started["id"]


def test_second_goal_report_in_one_turn_is_rejected(comms, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_COMMS_THREAD", "owner")
    comms.registry.declare(
        Thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    goal = invoke_tool(comms, "comms_set_goal", {"text": "finish this"})["goal"]
    comms.agents.begin_turn("owner", TurnId('d7f9429f60abc2787fc3e9717054ea41'))

    first = invoke_tool(
        comms,
        "comms_goal",
        {"goal_id": goal["id"], "status": "active", "progress": "one step"},
    )
    with pytest.raises(ValueError):
        invoke_tool(
            comms,
            "comms_goal",
            {"goal_id": goal["id"], "status": "active", "progress": "another step"},
        )
    assert first["goal"]["progress"] == "one step"
    assert comms.registry.require("owner").goal.progress == "one step"


def test_replacing_or_clearing_goal_cannot_reset_turn_report_guard(comms, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_COMMS_THREAD", "owner")
    comms.registry.declare(
        Thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    first = invoke_tool(comms, "comms_set_goal", {"text": "first"})["goal"]
    turn = TurnId('d7f9429f60abc2787fc3e9717054ea41')
    comms.agents.begin_turn("owner", turn)
    invoke_tool(
        comms,
        "comms_goal",
        {"goal_id": first["id"], "status": "active", "progress": "reported"},
    )

    replacement = invoke_tool(comms, "comms_set_goal", {"text": "replacement"})["goal"]
    replacement_goal_id = FieldCodec.decode(GoalId, replacement["id"])
    with pytest.raises(ValueError):
        invoke_tool(
            comms,
            "comms_goal",
            {"goal_id": replacement["id"], "status": "active", "progress": "second"},
        )

    comms.goals.update_goal(
        "owner", ClearGoalAction(expect=GoalPrecondition(goal_id=OneGoal(replacement_goal_id)))
    )
    after_clear = invoke_tool(comms, "comms_set_goal", {"text": "after clear"})["goal"]
    reopened = wire(comms.root)
    assert any(
        entry.reports_turn(ReportedTurn(turn))
        for entry in reopened.registry.goal_history("owner")
    )
    with pytest.raises(ValueError):
        invoke_tool(
            reopened,
            "comms_goal",
            {"goal_id": after_clear["id"], "status": "active", "progress": "third"},
        )
    assert comms.registry.require("owner").goal.progress == ""


def test_clearing_goal_releases_its_reserved_attempt(comms, monkeypatch):
    goal = _goal(comms, monkeypatch)
    goal_goal_id = FieldCodec.decode(GoalId, goal["id"])
    private = comms.root / "goal-private"
    store = comms.goals.open_goal_store()
    reservation = store.reserve(goal_goal_id, 1)

    comms.goals.update_goal("owner", ClearGoalAction(expect=GoalPrecondition(goal_id=OneGoal(goal_goal_id))))

    assert comms.registry.require("owner").goal == NoGoal()
    assert GoalAttemptStore.at(private).snapshot(goal_goal_id).lifecycle == CancelledGeneration()
    assert store.snapshot(goal_goal_id).attempt_id == reservation.attempt_id


def test_model_tool_cannot_resume_blocked_uncertain_goal(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    started_goal_id = FieldCodec.decode(GoalId, started["id"])
    blocked = comms.goals.update_goal(
        "owner",
        BlockedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(started_goal_id)), progress="uncertain"),
    )
    with pytest.raises(ValueError):
        invoke_tool(comms, "comms_resume_goal", {"goal_id": started["id"], "progress": "retry"})
    assert comms.registry.require("owner").goal == blocked


def test_resume_refuses_completed_or_replaced_goal(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    started_goal_id = FieldCodec.decode(GoalId, started["id"])
    comms.goals.update_goal(
        "owner", CompletedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(started_goal_id)))
    )
    with pytest.raises(ValueError):
        invoke_tool(comms, "comms_resume_goal", {"goal_id": started["id"], "progress": "no"})
    replacement = invoke_tool(comms, "comms_set_goal", {"text": "new goal"})["goal"]
    with pytest.raises(ValueError):
        invoke_tool(comms, "comms_resume_goal", {"goal_id": started["id"], "progress": "no"})
    assert comms.registry.require("owner").goal.id == replacement["id"]


def test_completed_goal_cannot_be_reactivated_by_ui_registry_action(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    started_goal_id = FieldCodec.decode(GoalId, started["id"])
    completed = comms.goals.update_goal(
        "owner", CompletedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(started_goal_id)))
    )
    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "owner", ActiveGoalAction(expect=GoalPrecondition(goal_id=OneGoal(started_goal_id)))
        )
    assert comms.registry.require("owner").goal == completed


@pytest.mark.parametrize("terminal", ["blocked", "completed"])
def test_terminal_goal_cannot_reactivate_through_pause(comms, monkeypatch, terminal):
    started = _goal(comms, monkeypatch)
    started_goal_id = FieldCodec.decode(GoalId, started["id"])
    private = comms.root / "goal-private"
    store = comms.goals.open_goal_store()
    reservation = store.reserve(started_goal_id, 1)
    if terminal == "blocked":
        reservation.fail(store, "uncertain turn")
    else:
        store.claim_launch(reservation).record_verified_completion(store, "finished")
    terminal_goal = comms.goals.update_goal(
        "owner",
        (
            BlockedGoalAction(
                expect=GoalPrecondition(goal_id=OneGoal(started_goal_id)),
                block_reason="Uncertain turn requires review",
            )
            if terminal == "blocked"
            else CompletedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(started_goal_id)))
        ),
    )

    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "owner", PausedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(started_goal_id)))
        )

    assert comms.registry.require("owner").goal == terminal_goal
    assert store.snapshot(started_goal_id).lifecycle == (
        BlockedGeneration() if terminal == "blocked" else CompletedGeneration()
    )


def test_same_value_goal_transition_bumps_durable_revision(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    started_goal_id = FieldCodec.decode(GoalId, started["id"])
    original = comms.registry.require("owner").goal
    assert original is not None and started["revision"] == original.revision
    first = comms.goals.update_goal(
        "owner", BlockedGoalAction(
            expect=GoalPrecondition(goal_id=OneGoal(started_goal_id)), progress="same")
    )
    assert first is not None
    second = wire(comms.root).goals.update_goal(
        "owner", BlockedGoalAction(
            expect=GoalPrecondition(goal_id=OneGoal(started_goal_id)), progress="same")
    )
    assert second is not None and second.revision == first.revision + 1
    assert wire(comms.root).registry.require("owner").goal == second


def test_exhausted_goal_revision_refuses_transition_without_write(comms):
    goal_id = GoalId.mint()
    comms.registry.declare(
        Thread(name='owner', tags=frozenset(), worktree='/wt', goal=Goal('work', goal_id,
            published_seq=0, revision=(1 << 63) - 1))
    )
    before = (comms.root / "registry.json").read_bytes()
    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "owner",
            BlockedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(goal_id)), progress="do not write"),
        )
    assert (comms.root / "registry.json").read_bytes() == before


def test_a_blocked_goal_is_retried_by_its_unit_or_commander_and_authorize_retry_guards_each(comms, monkeypatch):
    """No owner session exists: the unit retries through comms_goal, the commander retries and
    replaces through the runtime actor, and an unresolved attempt still refuses a retry."""
    goal_id = FieldCodec.decode(GoalId, _goal(comms, monkeypatch)["id"])
    store = comms.goals.open_goal_store()

    def failed_turn(diagnostic):
        generation = store.require_generation(goal_id)
        store.reserve(goal_id, generation.number).fail(store, diagnostic)
        assert isinstance(comms.registry.require("owner").goal.state, Active)

    failed_turn("Backend outcome uncertain.")
    retried = invoke_tool(comms, "comms_goal",
                          {"goal_id": goal_id, "status": "retry", "progress": "cause fixed"})
    goal = comms.registry.require("owner").goal
    goal.require_active()
    assert retried['goal']['state'] == {'kind': 'active'} and goal.progress == 'cause fixed'

    failed_turn("Provider failed again.")
    commander_retry = comms.goals.update_goal(
        "owner", RetryGoalAction(expect=GoalPrecondition(goal_id=OneGoal(goal_id))))
    commander_retry.require_active()
    assert comms.registry.require('owner').goal == commander_retry

    failed_turn("Provider failed a third time.")
    comms.goals.update_goal("owner", RetryGoalAction(expect=GoalPrecondition(goal_id=OneGoal(goal_id))))
    store.reserve(goal_id, store.require_generation(goal_id).number)
    with pytest.raises(ValueError):
        comms.goals.update_goal("owner", RetryGoalAction(expect=GoalPrecondition(goal_id=OneGoal(goal_id))))
    assert isinstance(comms.registry.require("owner").goal.state, Active)

    replaced = comms.goals.update_goal("owner", SetGoalAction(text="a fresh objective"))
    replaced.require_active()
    assert replaced.id != goal_id
    assert comms.registry.require("owner").goal == replaced
