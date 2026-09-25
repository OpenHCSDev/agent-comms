"""Same-ID, explicitly requested goal resume through the public tool boundary."""

import os

import pytest

from agent_comms import Goal, Thread
from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.operations import Comms
from agent_comms.tools import invoke_tool


def _goal(comms, monkeypatch):
    monkeypatch.delenv("PI_AGENT_ID", raising=False)
    monkeypatch.setenv("AGENT_COMMS_THREAD", "owner")
    comms.register(Thread(name="owner", tags=frozenset(), worktree="/wt"))
    return invoke_tool(comms, "comms_set_goal", {"text": "keep working"})["goal"]


def test_same_id_resume_from_paused(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    comms.update_goal("owner", "paused", goal_id=started["id"], progress="previous")

    result = invoke_tool(
        comms, "comms_resume_goal", {"goal_id": started["id"], "progress": "user resumed"}
    )["goal"]

    assert result["id"] == started["id"]
    assert result["text"] == started["text"]
    assert result["status"] == "active"
    assert result["progress"] == "user resumed"
    assert comms.registry.require("owner").goal.id == started["id"]


def test_second_goal_report_in_one_turn_is_rejected(comms, monkeypatch, tmp_path):
    monkeypatch.delenv("PI_AGENT_ID", raising=False)
    monkeypatch.setenv("AGENT_COMMS_THREAD", "owner")
    comms.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()))
    goal = invoke_tool(comms, "comms_set_goal", {"text": "finish this"})["goal"]
    comms.begin_turn("owner", "same-assistant-turn")

    first = invoke_tool(
        comms,
        "comms_goal",
        {"goal_id": goal["id"], "status": "active", "progress": "one step"},
    )
    with pytest.raises(ValueError, match="already reported"):
        invoke_tool(
            comms,
            "comms_goal",
            {"goal_id": goal["id"], "status": "active", "progress": "another step"},
        )
    assert first["goal"]["progress"] == "one step"
    assert comms.registry.require("owner").goal.progress == "one step"


def test_replacing_or_clearing_goal_cannot_reset_turn_report_guard(comms, monkeypatch, tmp_path):
    monkeypatch.delenv("PI_AGENT_ID", raising=False)
    monkeypatch.setenv("AGENT_COMMS_THREAD", "owner")
    comms.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()))
    first = invoke_tool(comms, "comms_set_goal", {"text": "first"})["goal"]
    comms.begin_turn("owner", "same-assistant-turn")
    invoke_tool(
        comms,
        "comms_goal",
        {"goal_id": first["id"], "status": "active", "progress": "reported"},
    )

    replacement = invoke_tool(comms, "comms_set_goal", {"text": "replacement"})["goal"]
    with pytest.raises(ValueError, match="already reported"):
        invoke_tool(
            comms,
            "comms_goal",
            {"goal_id": replacement["id"], "status": "active", "progress": "second"},
        )

    comms.update_goal("owner", "clear", goal_id=replacement["id"])
    after_clear = invoke_tool(comms, "comms_set_goal", {"text": "after clear"})["goal"]
    reopened = Comms(comms.root)
    assert reopened.registry.require("owner").last_goal_report_turn == "same-assistant-turn"
    with pytest.raises(ValueError, match="already reported"):
        invoke_tool(
            reopened,
            "comms_goal",
            {"goal_id": after_clear["id"], "status": "active", "progress": "third"},
        )
    assert comms.registry.require("owner").goal.progress == ""


def test_clearing_goal_releases_its_reserved_attempt(comms, monkeypatch):
    goal = _goal(comms, monkeypatch)
    private = comms.root / "goal-private"
    private.mkdir(mode=0o700)
    store = GoalAttemptStore.initialize(private)
    store.create_goal(goal["id"])
    reservation = store.reserve(goal["id"], 1)

    comms.update_goal("owner", "clear", goal_id=goal["id"])

    assert comms.registry.require("owner").goal is None
    assert GoalAttemptStore(private).snapshot(goal["id"]).state == "cancelled"
    assert store.snapshot(goal["id"]).attempt_id == reservation.attempt_id


def test_model_tool_cannot_resume_blocked_uncertain_goal(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    blocked = comms.update_goal("owner", "blocked", goal_id=started["id"], progress="uncertain")
    with pytest.raises(ValueError, match="cannot be resumed"):
        invoke_tool(comms, "comms_resume_goal", {"goal_id": started["id"], "progress": "retry"})
    assert comms.registry.require("owner").goal == blocked


def test_resume_refuses_completed_or_replaced_goal(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    comms.update_goal("owner", "completed", goal_id=started["id"])
    with pytest.raises(ValueError, match="cannot be resumed"):
        invoke_tool(comms, "comms_resume_goal", {"goal_id": started["id"], "progress": "no"})
    replacement = invoke_tool(comms, "comms_set_goal", {"text": "new goal"})["goal"]
    with pytest.raises(ValueError, match="cannot be resumed"):
        invoke_tool(comms, "comms_resume_goal", {"goal_id": started["id"], "progress": "no"})
    assert comms.registry.require("owner").goal.id == replacement["id"]


def test_completed_goal_cannot_be_reactivated_by_ui_registry_action(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    completed = comms.update_goal("owner", "completed", goal_id=started["id"])
    with pytest.raises(ValueError, match="completed goal"):
        comms.update_goal("owner", "active", goal_id=started["id"])
    assert comms.registry.require("owner").goal == completed


@pytest.mark.parametrize("terminal", ["blocked", "completed"])
def test_terminal_goal_cannot_reactivate_through_pause(comms, monkeypatch, terminal):
    started = _goal(comms, monkeypatch)
    private = comms.root / "goal-private"
    private.mkdir(mode=0o700)
    store = GoalAttemptStore.initialize(private)
    store.create_goal(started["id"])
    reservation = store.reserve(started["id"], 1)
    if terminal == "blocked":
        store.record_failed(reservation, "uncertain turn")
    else:
        store.record_verified_completion(store.claim_launch(reservation), "finished")
    terminal_goal = comms.update_goal("owner", terminal, goal_id=started["id"])

    with pytest.raises(ValueError, match="goal"):
        comms.update_goal("owner", "paused", goal_id=started["id"])

    assert comms.registry.require("owner").goal == terminal_goal
    assert store.snapshot(started["id"]).state == terminal


def test_resume_does_not_claim_success_after_concurrent_goal_change(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    comms.update_goal("owner", "paused", goal_id=started["id"], progress="before")
    real_update = comms.update_goal

    def race(name, action, **kwargs):
        real_update(name, "paused", goal_id=started["id"], progress="newer progress")
        return real_update(name, action, **kwargs)

    monkeypatch.setattr(comms, "update_goal", race)
    with pytest.raises(ValueError, match="changed during resume"):
        invoke_tool(
            comms,
            "comms_resume_goal",
            {"goal_id": started["id"], "progress": "stale progress"},
        )
    goal = comms.registry.require("owner").goal
    assert goal.status == "paused" and goal.progress == "newer progress"


@pytest.mark.parametrize("race_kind", ["replacement", "same_id_activation"])
def test_resume_losing_cas_never_reports_another_activations_success(comms, monkeypatch, race_kind):
    started = _goal(comms, monkeypatch)
    comms.update_goal("owner", "paused", goal_id=started["id"], progress="before")
    real_update = comms.update_goal
    requested_progress = "continue"

    def race(name, action, **kwargs):
        if race_kind == "replacement":
            replacement = real_update(name, "set", text="different goal")
            assert replacement is not None
            real_update(name, "active", goal_id=replacement.id, progress=requested_progress)
        else:
            real_update(name, "active", goal_id=started["id"], progress=requested_progress)
        return real_update(name, action, **kwargs)

    monkeypatch.setattr(comms, "update_goal", race)
    with pytest.raises(ValueError, match="changed during resume"):
        invoke_tool(
            comms,
            "comms_resume_goal",
            {"goal_id": started["id"], "progress": requested_progress},
        )
    current = comms.registry.require("owner").goal
    assert current.status == "active" and current.progress == requested_progress
    if race_kind == "replacement":
        assert current.id != started["id"]
    else:
        assert current.id == started["id"]


@pytest.mark.parametrize("prior", ["blocked", "paused"])
def test_resume_rejects_same_value_aba_across_registry_reopen(comms, monkeypatch, prior):
    started = _goal(comms, monkeypatch)
    comms.update_goal("owner", prior, goal_id=started["id"], progress="unchanged")
    saved = comms.registry.require("owner").goal
    assert saved is not None
    real_update = comms.update_goal
    other = Comms(comms.root)

    def race(name, action, **kwargs):
        assert action == "active"
        # Blocked goals cannot become active through an ordinary registry
        # transition; a second blocked report still exercises the ABA CAS.
        other.update_goal(
            name,
            "blocked" if prior == "blocked" else "active",
            goal_id=saved.id,
            progress="newer report",
        )
        other.update_goal(name, prior, goal_id=saved.id, progress="unchanged")
        now = Comms(comms.root).registry.require(name).goal
        assert now is not None
        assert (now.id, now.status, now.progress) == (saved.id, saved.status, saved.progress)
        return real_update(name, action, **kwargs)

    monkeypatch.setattr(comms, "update_goal", race)
    with pytest.raises(ValueError, match="changed during resume"):
        if prior == "paused":
            invoke_tool(
                comms,
                "comms_resume_goal",
                {"goal_id": saved.id, "progress": "stale second resume"},
            )
        else:
            # The model tool cannot resume blocked work. Even a future
            # authenticated human recovery caller must honor this CAS.
            comms.update_goal(
                "owner",
                "active",
                goal_id=saved.id,
                expected_status=prior,
                expected_goal=saved,
                progress="stale second resume",
            )
    final = Comms(comms.root).registry.require("owner").goal
    assert final is not None and final.status == prior and final.progress == "unchanged"
    assert final.revision == saved.revision + 2


def test_same_value_goal_transition_bumps_durable_revision(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    original = comms.registry.require("owner").goal
    assert original is not None and started["revision"] == original.revision
    first = comms.update_goal("owner", "blocked", goal_id=started["id"], progress="same")
    assert first is not None
    second = Comms(comms.root).update_goal(
        "owner", "blocked", goal_id=started["id"], progress="same"
    )
    assert second is not None and second.revision == first.revision + 1
    assert Comms(comms.root).registry.require("owner").goal == second


def test_automatic_failure_block_advances_goal_revision(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    snapshot = comms.registry.require("owner").goal
    assert snapshot is not None
    newer = Comms(comms.root).update_goal(
        "owner", "active", goal_id=started["id"], progress="verified newer step"
    )
    assert newer is not None
    blocked = comms.block_goal_after_failed_turn(
        "owner",
        started_goal=snapshot,
        expected_worktree="/wt",
        diagnostic="Backend outcome uncertain.",
    )
    assert blocked is not None and blocked.status == "blocked"
    assert blocked.revision == newer.revision + 1
    assert blocked.progress.startswith("verified newer step\n\n")
    assert Comms(comms.root).registry.require("owner").goal == blocked


def test_automatic_failure_block_preserves_existing_goal_progress(comms, monkeypatch):
    started = _goal(comms, monkeypatch)
    goal = comms.update_goal(
        "owner", "active", goal_id=started["id"], progress="Verified first step"
    )
    assert goal is not None

    blocked = comms.block_goal_after_failed_turn(
        "owner",
        started_goal=goal,
        expected_worktree="/wt",
        diagnostic="Backend outcome uncertain.",
    )

    assert blocked is not None
    assert blocked.status == "blocked"
    assert blocked.progress == "Verified first step\n\nBackend outcome uncertain."


def test_exhausted_goal_revision_refuses_transition_without_write(comms):
    comms.register(
        Thread(
            name="owner",
            tags=frozenset(),
            worktree="/wt",
            goal=Goal("work", "goal-id", revision=(1 << 63) - 1),
        )
    )
    before = (comms.root / "registry.json").read_bytes()
    with pytest.raises(ValueError, match="revision"):
        comms.update_goal("owner", "blocked", goal_id="goal-id", progress="do not write")
    assert (comms.root / "registry.json").read_bytes() == before
