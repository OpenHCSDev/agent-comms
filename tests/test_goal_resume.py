"""Same-ID, explicitly requested goal resume through the public tool boundary."""

import os

import pytest

from agent_comms import Goal, Thread
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
        other.update_goal(name, "active", goal_id=saved.id, progress="first owner resumed")
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
