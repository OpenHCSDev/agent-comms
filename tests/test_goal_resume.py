"""Same-ID, explicitly requested goal resume through the public tool boundary."""

import os

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
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
from agent_comms.tools import invoke_tool
from agent_comms.tool_caller import ThreadCaller
from functools import partial

# Every case in this module exercises the explicitly declared owner.
invoke_tool = partial(invoke_tool, caller=ThreadCaller("owner"))


def _goal(comms, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_THREAD", "owner")
    comms.registry.declare(comms.registry.new_thread(name='owner', tags=frozenset(), worktree='/wt'))
    return invoke_tool(comms, "comms_set_goal", {"text": "keep working"})["goal"]


def test_same_id_resume_from_paused(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    comms.goals.update_goal(
        "owner",
        PausedGoalAction(expect=GoalPrecondition(goal_id=started["id"]), progress="previous"),
    )

    result = invoke_tool(
        comms, "comms_resume_goal", {"goal_id": started["id"], "progress": "user resumed"}
    )["goal"]

    assert result["id"] == started["id"]
    assert result["text"] == started["text"]
    FieldCodec.decode(Goal, result).state.require_active()
    assert result["progress"] == "user resumed"
    assert comms.registry.require("owner").goal.id == started["id"]


def test_second_goal_report_in_one_turn_is_rejected(comms, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_COMMS_THREAD", "owner")
    comms.registry.declare(
        comms.registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    goal = invoke_tool(comms, "comms_set_goal", {"text": "finish this"})["goal"]
    comms.agents.begin_turn("owner", "same-assistant-turn")

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
        comms.registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    first = invoke_tool(comms, "comms_set_goal", {"text": "first"})["goal"]
    comms.agents.begin_turn("owner", "same-assistant-turn")
    invoke_tool(
        comms,
        "comms_goal",
        {"goal_id": first["id"], "status": "active", "progress": "reported"},
    )

    replacement = invoke_tool(comms, "comms_set_goal", {"text": "replacement"})["goal"]
    with pytest.raises(ValueError):
        invoke_tool(
            comms,
            "comms_goal",
            {"goal_id": replacement["id"], "status": "active", "progress": "second"},
        )

    comms.goals.update_goal(
        "owner", ClearGoalAction(expect=GoalPrecondition(goal_id=replacement["id"]))
    )
    after_clear = invoke_tool(comms, "comms_set_goal", {"text": "after clear"})["goal"]
    reopened = Comms(comms.root)
    assert any(
        entry.reports_turn("same-assistant-turn")
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
    private = comms.root / "goal-private"
    store = comms.goals.open_goal_store()
    reservation = store.reserve(goal["id"], 1)

    comms.goals.update_goal("owner", ClearGoalAction(expect=GoalPrecondition(goal_id=goal["id"])))

    assert comms.registry.require("owner").goal == NoGoal()
    assert GoalAttemptStore(private).snapshot(goal["id"]).lifecycle == CancelledGeneration()
    assert store.snapshot(goal["id"]).attempt_id == reservation.attempt_id


def test_model_tool_cannot_resume_blocked_uncertain_goal(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    blocked = comms.goals.update_goal(
        "owner",
        BlockedGoalAction(expect=GoalPrecondition(goal_id=started["id"]), progress="uncertain"),
    )
    with pytest.raises(ValueError):
        invoke_tool(comms, "comms_resume_goal", {"goal_id": started["id"], "progress": "retry"})
    assert comms.registry.require("owner").goal == blocked


def test_resume_refuses_completed_or_replaced_goal(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    comms.goals.update_goal(
        "owner", CompletedGoalAction(expect=GoalPrecondition(goal_id=started["id"]))
    )
    with pytest.raises(ValueError):
        invoke_tool(comms, "comms_resume_goal", {"goal_id": started["id"], "progress": "no"})
    replacement = invoke_tool(comms, "comms_set_goal", {"text": "new goal"})["goal"]
    with pytest.raises(ValueError):
        invoke_tool(comms, "comms_resume_goal", {"goal_id": started["id"], "progress": "no"})
    assert comms.registry.require("owner").goal.id == replacement["id"]


def test_completed_goal_cannot_be_reactivated_by_ui_registry_action(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    completed = comms.goals.update_goal(
        "owner", CompletedGoalAction(expect=GoalPrecondition(goal_id=started["id"]))
    )
    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "owner", ActiveGoalAction(expect=GoalPrecondition(goal_id=started["id"]))
        )
    assert comms.registry.require("owner").goal == completed


@pytest.mark.parametrize("terminal", ["blocked", "completed"])
def test_terminal_goal_cannot_reactivate_through_pause(comms, monkeypatch, terminal):
    started = _goal(comms, monkeypatch)
    private = comms.root / "goal-private"
    store = comms.goals.open_goal_store()
    reservation = store.reserve(started["id"], 1)
    if terminal == "blocked":
        reservation.fail(store, "uncertain turn")
    else:
        store.claim_launch(reservation).record_verified_completion(store, "finished")
    terminal_goal = comms.goals.update_goal(
        "owner",
        (
            BlockedGoalAction(
                expect=GoalPrecondition(goal_id=started["id"]),
                block_reason="Uncertain turn requires review",
            )
            if terminal == "blocked"
            else CompletedGoalAction(expect=GoalPrecondition(goal_id=started["id"]))
        ),
    )

    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "owner", PausedGoalAction(expect=GoalPrecondition(goal_id=started["id"]))
        )

    assert comms.registry.require("owner").goal == terminal_goal
    assert store.snapshot(started["id"]).lifecycle == (
        BlockedGeneration() if terminal == "blocked" else CompletedGeneration()
    )


def test_same_value_goal_transition_bumps_durable_revision(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    original = comms.registry.require("owner").goal
    assert original is not None and started["revision"] == original.revision
    first = comms.goals.update_goal(
        "owner", BlockedGoalAction(expect=GoalPrecondition(goal_id=started["id"]), progress="same")
    )
    assert first is not None
    second = Comms(comms.root).goals.update_goal(
        "owner", BlockedGoalAction(expect=GoalPrecondition(goal_id=started["id"]), progress="same")
    )
    assert second is not None and second.revision == first.revision + 1
    assert Comms(comms.root).registry.require("owner").goal == second


def test_automatic_failure_block_advances_goal_revision(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    snapshot = comms.registry.require("owner").goal
    assert snapshot is not None
    newer = Comms(comms.root).goals.update_goal(
        "owner",
        ActiveGoalAction(
            expect=GoalPrecondition(goal_id=started["id"]), progress="verified newer step"
        ),
    )
    assert newer is not None
    blocked = comms.goals.block_goal_after_failed_turn(
        "owner",
        started_goal=snapshot,
        expected_worktree="/wt",
        diagnostic="Backend outcome uncertain.",
    )
    assert blocked is not None and blocked.state.declared_name == "blocked"
    assert blocked.revision == newer.revision + 1
    assert blocked.progress.startswith("verified newer step\n\n")
    assert Comms(comms.root).registry.require("owner").goal == blocked


def test_automatic_failure_block_preserves_existing_goal_progress(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    goal = comms.goals.update_goal(
        "owner",
        ActiveGoalAction(
            expect=GoalPrecondition(goal_id=started["id"]), progress="Verified first step"
        ),
    )
    assert goal is not None

    blocked = comms.goals.block_goal_after_failed_turn(
        "owner",
        started_goal=goal,
        expected_worktree="/wt",
        diagnostic="Backend outcome uncertain.",
    )

    assert blocked is not None
    assert blocked.state.declared_name == "blocked"
    assert blocked.progress == "Verified first step\n\nBackend outcome uncertain."


def test_exhausted_goal_revision_refuses_transition_without_write(comms):
    comms.registry.declare(
        comms.registry.new_thread(name='owner', tags=frozenset(), worktree='/wt', goal=Goal('work', 'goal-id', published_seq=0, revision=(1 << 63) - 1))
    )
    before = (comms.root / "registry.json").read_bytes()
    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "owner",
            BlockedGoalAction(expect=GoalPrecondition(goal_id="goal-id"), progress="do not write"),
        )
    assert (comms.root / "registry.json").read_bytes() == before


def test_a_blocked_goal_is_retried_by_its_unit_or_commander_and_authorize_retry_guards_each(comms, monkeypatch):
    """No owner session exists: the unit retries through comms_goal, the commander retries and
    replaces through the runtime actor, and an unresolved attempt still refuses a retry."""
    goal_id = _goal(comms, monkeypatch)["id"]
    store = comms.goals.open_goal_store()

    def failed_turn(diagnostic):
        started = comms.registry.require("owner").goal
        blocked = comms.goals.block_goal_after_failed_turn(
            "owner", started_goal=started, expected_worktree="/wt", diagnostic=diagnostic)
        assert blocked.state.declared_name == "blocked"
        return blocked

    failed_turn("Backend outcome uncertain.")
    retried = invoke_tool(comms, "comms_goal",
                          {"goal_id": goal_id, "status": "retry", "progress": "cause fixed"})
    goal = comms.registry.require("owner").goal
    goal.state.require_active()
    assert retried['goal']['state'] == {'kind': 'active'} and goal.progress == 'cause fixed'

    failed_turn("Provider failed again.")
    commander_retry = comms.goals.update_goal(
        "owner", RetryGoalAction(expect=GoalPrecondition(goal_id=goal_id)))
    commander_retry.state.require_active()
    assert comms.registry.require('owner').goal == commander_retry

    failed_turn("Provider failed a third time.")
    store.reserve(goal_id, store.require_generation(goal_id).number)
    with pytest.raises(ValueError):
        comms.goals.update_goal("owner", RetryGoalAction(expect=GoalPrecondition(goal_id=goal_id)))
    assert comms.registry.require("owner").goal.state.declared_name == "blocked"

    replaced = comms.goals.update_goal("owner", SetGoalAction(text="a fresh objective"))
    replaced.state.require_active()
    assert replaced.id != goal_id
    assert comms.registry.require("owner").goal == replaced
