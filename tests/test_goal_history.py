"""Goal history uses the current registry goal as its readback authority."""

from __future__ import annotations


from agent_comms.repo import Checkout
import json
import os
from dataclasses import replace

import pytest

from agent_comms.child_process import Platform
from agent_comms.comms import Comms, wire
from conftest import installed_comms
from test_locked_store import _hold_lock, event, process
from agent_comms.goal_actions import (
    ClearGoalAction,
    CompletedGoalAction,
    GoalPrecondition,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.goal_history import (
    BaselineRevision,
    GoalHistoryError,
    GoalHistoryStore,
    ObservedGapRevision,
    TransitionRevision,
)
from agent_comms.goal_scope import OneGoal
from agent_comms.goals import NoGoal
from agent_comms.store_files import ExclusiveLock
from agent_comms.goal_states import ActiveGoal
from agent_comms.tool_request import ToolRequest
from agent_comms.tool_caller import ThreadCaller
from agent_comms.threads import Thread


def _wire(tmp_path) -> Comms:
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(Thread('worker', frozenset(), Checkout.from_wire(str(tmp_path))))
    return comms


def test_history_file_fsync_reopens_with_supported_platform_flags(tmp_path):
    path = tmp_path / "history.sqlite3"
    path.write_bytes(b"journal bytes")
    # Windows' os.fsync delegates to the CRT, which rejects O_RDONLY fds.
    Platform.current().sync_file(path)
    assert path.read_bytes() == b"journal bytes"


def test_goal_history_records_transitions_replacement_clear_and_rename(tmp_path, monkeypatch):
    comms = _wire(tmp_path)
    first = comms.goals.update_goal("worker", SetGoalAction(text="Ask @reviewer about the release"))
    paused = comms.goals.update_goal(
        "worker", PausedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(first.id)), progress="Waiting")
    )
    # ACP and other callers also write the declaration directly.
    thread = comms.registry.require("worker")
    resumed = replace(paused, state=ActiveGoal(), revision=paused.revision + 1)
    comms.registry.register(replace(thread, goal=resumed), comms.registry.status("worker"))
    monkeypatch.setenv("AGENT_COMMS_THREAD", "worker")
    comms.threads.rename_self("reviewer", caller=ThreadCaller("worker"))
    completed = comms.goals.update_goal(
        "reviewer", CompletedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(first.id)))
    )
    second = comms.goals.update_goal("reviewer", SetGoalAction(text="Start a new objective"))
    comms.goals.update_goal("reviewer", ClearGoalAction(expect=GoalPrecondition(goal_id=OneGoal(second.id))))

    reopened = wire(comms.root)
    entries = reopened.goals.goal_history("worker")
    assert len(entries) == 6
    assert all(entry.kind is TransitionRevision for entry in entries)
    assert [entry.after for entry in entries] == [first, paused, resumed, completed, second, NoGoal()]
    assert entries[0].after.text == "Ask @reviewer about the release"
    assert entries[4].before == completed
    assert entries[5].before == second
    assert reopened.goals.goal_history("reviewer", OneGoal(first.id)) == entries[:5]
    assert reopened.goals.goal_history("reviewer", OneGoal(second.id)) == entries[4:]

    history_tool = next(
        tool
        for tool in ToolRequest.members_with(ToolRequest)
        if tool.declared_name == "comms_goal_history"
    )
    result = history_tool.invoke(reopened, {"goal_id": first.id}, caller=ThreadCaller("worker"))
    assert [row["after"]["revision"] for row in result["history"] if row["after"]] == [
        1,
        2,
        3,
        4,
        1,
    ]


def test_existing_goal_seeds_only_current_observed_baseline(tmp_path):
    comms = _wire(tmp_path)
    current = comms.goals.update_goal("worker", SetGoalAction(text="Existing goal"))
    (
        comms.root / "goal_history.sqlite3"
    ).unlink()  # model missing history beside an existing current goal

    reopened = wire(comms.root)
    baseline = reopened.goals.goal_history("worker")
    assert len(baseline) == 1
    assert baseline[0].kind is BaselineRevision
    assert baseline[0].before == NoGoal() and baseline[0].after == current
    assert wire(comms.root).goals.goal_history("worker") == baseline
    paused = reopened.goals.update_goal(
        "worker", PausedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(current.id)))
    )
    entries = wire(comms.root).goals.goal_history("worker")
    assert len(entries) == 2
    assert entries[0].kind is BaselineRevision
    assert entries[1].kind is TransitionRevision
    assert entries[-1].before == current and entries[-1].after == paused


def test_crash_before_registry_write_does_not_expose_history_intent(tmp_path, monkeypatch):
    comms = _wire(tmp_path)
    current = comms.goals.update_goal("worker", SetGoalAction(text="Keep current"))
    original_replace = os.replace

    def failed_registry_write(source, destination, **directories):
        if destination == comms.registry.store.path:
            raise OSError("simulated registry write failure")
        return original_replace(source, destination, **directories)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", failed_registry_write)
        with pytest.raises(OSError):
            comms.goals.update_goal(
                "worker", PausedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(current.id)))
            )

    reopened = wire(comms.root)
    assert reopened.registry.require("worker").goal == current
    entries = reopened.goals.goal_history("worker")
    assert len(entries) == 1 and entries[0].after == current


@pytest.mark.parametrize("failure", ["before_history_commit", "after_history_commit"])
def test_crash_after_registry_write_reconciles_pending_history(tmp_path, monkeypatch, failure):
    comms = _wire(tmp_path)
    current = comms.goals.update_goal("worker", SetGoalAction(text="Keep current"))

    commit = GoalHistoryStore.commit

    def fail_commit(store, sequence):
        if failure == "before_history_commit":
            raise OSError("lost history ACK")

        def fail_sync():
            raise OSError("lost history ACK")

        with monkeypatch.context() as patch:
            patch.setattr(store, "_sync", fail_sync)
            commit(store, sequence)

    with monkeypatch.context() as patch:
        patch.setattr(GoalHistoryStore, "commit", fail_commit)
        expected = OSError if failure == "before_history_commit" else GoalHistoryError
        with pytest.raises(expected):
            comms.goals.update_goal(
                "worker", PausedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(current.id)))
            )

    reopened = wire(comms.root)
    paused = reopened.registry.require("worker").goal
    assert paused is not None and paused.state.declared_name == "paused"
    entries = reopened.goals.goal_history("worker")
    assert len(entries) == 2
    assert all(entry.kind is TransitionRevision for entry in entries)
    assert entries[-1].before == current and entries[-1].after == paused


def test_unjournaled_current_registry_change_is_labeled_observed_gap(tmp_path):
    comms = _wire(tmp_path)
    current = comms.goals.update_goal("worker", SetGoalAction(text="Known version"))
    registry_path = comms.registry.store.path
    raw = json.loads(registry_path.read_text())
    raw["threads"]["worker"]["goal"]["text"] = "Unjournaled change"
    raw["threads"]["worker"]["goal"]["revision"] = current.revision + 2
    registry_path.write_text(json.dumps(raw))

    reopened = wire(comms.root)
    entries = reopened.goals.goal_history("worker")
    assert len(entries) == 2
    assert entries[0].kind is TransitionRevision
    assert entries[1].kind is ObservedGapRevision
    assert entries[-1].before == current
    assert entries[-1].after.text == "Unjournaled change"
    assert len(wire(comms.root).goals.goal_history("worker")) == 2


def test_goal_history_reads_while_another_process_writes_the_registry(tmp_path):
    comms = _wire(tmp_path)
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Ask @reviewer about the release"))
    ready, release = event(), event()
    with process(_hold_lock, comms.registry.store.path, ExclusiveLock(), ready, release):
        try:
            assert ready.wait(5)
            entries = comms.goals.goal_history("worker")
        finally:
            release.set()
    assert [entry.after for entry in entries][-1:] == [goal]


def test_a_lock_free_read_leaves_an_in_flight_intent_to_its_writer(tmp_path):
    comms = _wire(tmp_path)
    first = comms.goals.update_goal("worker", SetGoalAction(text="Ask @reviewer about the release"))
    thread = comms.registry.require("worker")
    history = GoalHistoryStore(comms.registry.store.path)
    # A writer between its intent and its registry save: the registry still shows ``first``.
    intent = history.begin(thread.incarnation.wire_birth, first, NoGoal())
    assert comms.goals.goal_history("worker")[-1].after == first
    history.commit(intent)
    assert (TransitionRevision, first, NoGoal()) in [
        (entry.kind, entry.before, entry.after) for entry in comms.goals.goal_history("worker")]
