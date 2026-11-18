"""Current activity has one row; writers serialize each transition exactly once."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from threading import Barrier
from unittest.mock import patch

import pytest

from agent_comms.registry.activity import Activity, ActivityRow, ActivityState, ActivityStore


@pytest.fixture
def activity_store(tmp_path):
    return ActivityStore(tmp_path)


def test_emit_and_current_return_the_declared_activity_row(activity_store):
    first = Activity("worker", ActivityState.THINKING)
    activity_store.emit(first)
    assert activity_store.current("worker") == ActivityRow.of(first)
    latest = first.with_status(ActivityState.WORKING, "read source")
    activity_store.emit(latest)
    assert ActivityStore(activity_store.root).current("worker") == ActivityRow.of(latest)
    with activity_store.snapshot() as db:
        assert ActivityRow.count(db) == 1


def test_retiring_activity_removes_the_row(activity_store):
    activity_store.emit(Activity("worker", ActivityState.WORKING, "read source"))
    activity_store.emit(Activity("worker", ActivityState.DELETED))
    assert activity_store.current("worker") == Activity.absent("worker")


def test_rename_moves_current_activity_without_changing_its_fields(activity_store):
    event = Activity("before", ActivityState.WORKING, "read source")
    activity_store.emit(event)
    activity_store.rename_thread("before", "after")
    assert activity_store.current("before") == Activity.absent("before")
    assert activity_store.current("after") == ActivityRow.of(replace(event, thread="after"))
    with activity_store.snapshot() as db:
        assert ActivityRow.count(db) == 1


def test_cohort_capture_batches_original_rows_and_preserves_expiry(activity_store):
    names = tuple(f"worker-{index}" for index in range(66))
    for index, name in enumerate(names):
        activity_store.emit(Activity(name, ActivityState.THINKING,
                                     timestamp=0.0 if index < 2 else 1000.0))
    with activity_store.snapshot() as db:
        queries = []
        db.set_trace_callback(queries.append)
        with patch.object(activity_store, "snapshot", return_value=nullcontext(db)), patch(
            "agent_comms.registry.activity.time.time", return_value=1001.0
        ):
            capture = activity_store.captured_for(
                (*names, names[2], "missing"), active=frozenset((names[1],)))
        db.set_trace_callback(None)
    assert tuple(capture.activity) == (*names, "missing")
    assert capture.activity[names[0]].state is ActivityState.IDLE
    assert capture.activity[names[0]].timestamp == 0.0
    assert capture.activity[names[1]].state is ActivityState.THINKING
    assert capture.activity["missing"] == Activity.absent("missing")
    assert capture.expires_at == 1120.0
    reads = [sql for sql in queries if sql.startswith(("SELECT", "WITH"))
             and f'"{ActivityRow.declared_name}"' in sql]
    assert len(reads) == 1


def test_concurrent_update_applies_each_transition_once(activity_store):
    activity_store.emit(Activity("worker", ActivityState.WORKING, "0"))
    start = Barrier(2)

    def write():
        observations = []

        def advance(current):
            observations.append(current.detail)
            return current.with_status(ActivityState.WORKING, str(int(current.detail) + 1))

        start.wait(timeout=5)
        assert ActivityStore(activity_store.root).update("worker", advance)
        return observations

    with ThreadPoolExecutor(max_workers=2) as writers:
        written = tuple(writers.map(lambda _: write(), range(2)))
    assert sorted(written) == [["0"], ["1"]]
    assert activity_store.current("worker").detail == "2"
