"""Goal history uses the current registry goal as its readback authority."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_comms import Thread, declarations
from agent_comms.goal_history import GoalHistoryError, GoalHistoryStore
from agent_comms.operations import Comms
from agent_comms.tools import TOOLS


def _wire(tmp_path) -> Comms:
    comms = Comms(tmp_path / "wire")
    comms.register(Thread("worker", frozenset(), str(tmp_path)))
    return comms


def test_history_file_fsync_reopens_with_supported_platform_flags(tmp_path):
    path = tmp_path / "history.sqlite3"
    path.write_bytes(b"journal bytes")
    # Windows' os.fsync delegates to the CRT, which rejects O_RDONLY fds.
    GoalHistoryStore._sync_file(path)
    assert path.read_bytes() == b"journal bytes"


def test_goal_history_records_transitions_replacement_clear_and_rename(tmp_path, monkeypatch):
    comms = _wire(tmp_path)
    first = comms.update_goal("worker", "set", text="Ask @reviewer about the release")
    paused = comms.update_goal("worker", "paused", goal_id=first.id, progress="Waiting")
    # ACP and other callers also write the declaration directly.
    thread = comms.registry.require("worker")
    resumed = replace(paused, status="active", revision=paused.revision + 1)
    comms.registry.register(replace(thread, goal=resumed), comms.registry.status("worker"))
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    comms.rename_self("reviewer")
    completed = comms.update_goal("reviewer", "completed", goal_id=first.id)
    second = comms.update_goal("reviewer", "set", text="Start a new objective")
    comms.update_goal("reviewer", "clear", goal_id=second.id)

    reopened = Comms(comms.root)
    entries = reopened.goal_history("worker")
    assert [entry.kind for entry in entries] == ["transition"] * 6
    assert [entry.after for entry in entries] == [first, paused, resumed, completed, second, None]
    assert entries[0].after.text == "Ask @reviewer about the release"
    assert entries[4].before == completed
    assert entries[5].before == second
    assert reopened.goal_history("reviewer", goal_id=first.id) == entries[:5]
    assert reopened.goal_history("reviewer", goal_id=second.id) == entries[4:]

    history_tool = next(tool for tool in TOOLS if tool.name == "comms_goal_history")
    result = history_tool.invoke(reopened, {"goal_id": first.id})
    assert [row["after"]["revision"] for row in result["history"] if row["after"]] == [
        1,
        2,
        3,
        4,
        1,
    ]


def test_existing_goal_seeds_only_current_observed_baseline(tmp_path):
    comms = _wire(tmp_path)
    current = comms.update_goal("worker", "set", text="Existing goal")
    (comms.root / "goal_history.sqlite3").unlink()  # model a pre-history registry

    reopened = Comms(comms.root)
    baseline = reopened.goal_history("worker")
    assert len(baseline) == 1
    assert baseline[0].kind == "baseline"
    assert baseline[0].before is None and baseline[0].after == current
    assert Comms(comms.root).goal_history("worker") == baseline
    paused = reopened.update_goal("worker", "paused", goal_id=current.id)
    entries = Comms(comms.root).goal_history("worker")
    assert [entry.kind for entry in entries] == ["baseline", "transition"]
    assert entries[-1].before == current and entries[-1].after == paused


def test_crash_before_registry_write_does_not_expose_history_intent(tmp_path, monkeypatch):
    comms = _wire(tmp_path)
    current = comms.update_goal("worker", "set", text="Keep current")
    original_write = declarations._atomic_write_text

    def failed_registry_write(path, text, *, fsync_parent=False):
        if path == comms.registry._path:
            raise OSError("simulated registry write failure")
        return original_write(path, text, fsync_parent=fsync_parent)

    with monkeypatch.context() as patch:
        patch.setattr(declarations, "_atomic_write_text", failed_registry_write)
        with pytest.raises(OSError, match="registry write failure"):
            comms.update_goal("worker", "paused", goal_id=current.id)

    reopened = Comms(comms.root)
    assert reopened.registry.require("worker").goal == current
    entries = reopened.goal_history("worker")
    assert len(entries) == 1 and entries[0].after == current


@pytest.mark.parametrize("failure", ["before_history_commit", "after_history_commit"])
def test_crash_after_registry_write_reconciles_pending_history(tmp_path, monkeypatch, failure):
    comms = _wire(tmp_path)
    current = comms.update_goal("worker", "set", text="Keep current")

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
            comms.update_goal("worker", "paused", goal_id=current.id)

    reopened = Comms(comms.root)
    paused = reopened.registry.require("worker").goal
    assert paused is not None and paused.status == "paused"
    entries = reopened.goal_history("worker")
    assert [entry.kind for entry in entries] == ["transition", "transition"]
    assert entries[-1].before == current and entries[-1].after == paused


def test_old_registry_writer_goal_change_is_labeled_observed_gap(tmp_path):
    comms = _wire(tmp_path)
    current = comms.update_goal("worker", "set", text="Known version")
    registry_path = comms.registry._path
    raw = json.loads(registry_path.read_text())
    raw["threads"]["worker"]["goal"]["text"] = "Old writer changed this"
    raw["threads"]["worker"]["goal"]["revision"] = current.revision + 2
    registry_path.write_text(json.dumps(raw))

    reopened = Comms(comms.root)
    entries = reopened.goal_history("worker")
    assert [entry.kind for entry in entries] == ["transition", "observed_gap"]
    assert entries[-1].before == current
    assert entries[-1].after.text == "Old writer changed this"
    assert len(Comms(comms.root).goal_history("worker")) == 2
