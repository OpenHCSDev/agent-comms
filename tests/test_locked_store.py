"""Real canonical locks, durable GoalWaits bytes, and bounded process tests."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
from contextlib import contextmanager
from dataclasses import replace

import pytest

from agent_comms import locked_store
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_history import GoalHistoryError, GoalHistoryStore
from agent_comms.goal_pauses import GoalPauseEvent, GoalPauseEvents
from agent_comms.goal_presentation import GoalWaitTarget
from agent_comms.goal_states import OwnerPause, PausedGoal, RuntimePause
from agent_comms.goal_waits import GoalWait, GoalWaits
from agent_comms.goals import Goal
from agent_comms.locked_store import LockedStore
from agent_comms.store_files import _store_lock


def wait(goal_id="goal"):
    return GoalWait(
        goal_id,
        "wait",
        2,
        7,
        (GoalWaitTarget("worker", 123.0),),
        122.0,
        (3,),
        "turn",
        4,
    )



def _hold_lock(path, shared, ready, release):
    with _store_lock(path, shared=shared):
        ready.set()
        if not release.wait(10):
            raise TimeoutError("lock holder was not released")


def _read(path, started, done):
    started.set()
    GoalWaits(path).read()
    done.set()


def _record(path, start, prefix):
    if not start.wait(10):
        raise TimeoutError("writer was not started")
    for index in range(12):
        GoalWaits(path).record(wait(f"{prefix}-{index}"))


def _record_under_wire(path, ready):
    with _store_lock(path.parent / "wire"):
        store = GoalWaits(path)
        store.record(wait())
        assert store.read() == {"goal": wait()}
        assert store.clear("goal", wait_id="wait")
    ready.set()


@contextmanager
def process(target, *args):
    # spawn does not inherit a parent's flock descriptor or pytest threads.
    child = multiprocessing.get_context("spawn").Process(target=target, args=args)
    child.start()
    try:
        yield child
        child.join(10)
        assert not child.is_alive(), "bounded worker did not finish"
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
            child.join(3)
        if child.is_alive():
            child.kill()
            child.join(3)
        child.close()


def event():
    return multiprocessing.get_context("spawn").Event()


def test_abstract_parent_and_real_adopter(tmp_path):
    with pytest.raises(TypeError, match="abstract"):
        LockedStore(tmp_path / "document")
    assert GoalWaits.read is LockedStore.read
    assert GoalWaits.update is LockedStore.update


def test_current_wait_persistence_and_noop(tmp_path):
    store = GoalWaits(tmp_path / "goal_waits.json")
    assert store.read() == {}
    assert not store.path.exists()
    assert not store.clear("absent")
    assert not store.path.exists()
    store.record(wait())
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.read() == {"goal": wait()}
    before = store.path.stat()
    assert not store.clear("goal", wait_id="stale")
    assert store.path.stat() == before
    assert store.clear("goal", wait_id="wait")
    assert store.read() == {}


def test_mode_preserved_and_new_inode_published(tmp_path):
    store = GoalWaits(tmp_path / "goal_waits.json")
    store.record(wait())
    store.path.chmod(0o640)
    inode = store.path.stat().st_ino
    store.record(replace(wait(), revision=3))
    assert store.path.stat().st_ino != inode
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o640
    assert store.read()["goal"].revision == 3
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        ".goal_waits.json.lock",
        "goal_waits.json",
    ]


def test_unknown_wait_fields_are_rejected_without_rewriting_saved_data(tmp_path):
    store = GoalWaits(tmp_path / "goal_waits.json")
    data = FieldCodec.encode({"goal": wait()})
    data["goal"]["future_field"] = {"not_authoritative_here": True}
    store.path.write_text(json.dumps(data))
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="Unknown fields"):
        store.read()
    with pytest.raises(ValueError, match="Unknown fields"):
        store.record(wait())
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("stage", ["file_sync", "replace", "directory_sync"])
def test_write_failure_preserves_previous_bytes(tmp_path, monkeypatch, existing, stage):
    store = GoalWaits(tmp_path / "goal_waits.json")
    if existing:
        store.record(wait())
        store.path.chmod(0o640)
    original_fsync = os.fsync
    original_replace = locked_store._replace_snapshot
    failed = False

    def fsync(fd):
        nonlocal failed
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if not failed and (
            (stage == "file_sync" and not directory) or (stage == "directory_sync" and directory)
        ):
            failed = True
            raise OSError("injected sync failure")
        return original_fsync(fd)

    def publish(source, target):
        if stage == "replace":
            raise OSError("injected replace failure")
        return original_replace(source, target)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(locked_store, "_replace_snapshot", publish)
    with pytest.raises(OSError, match="injected"):
        store.record(replace(wait(), revision=999))
    if existing:
            assert stat.S_IMODE(store.path.stat().st_mode) == 0o640
    else:
        assert not store.path.exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.previous"))


def test_callback_and_encoding_fail_before_publication(tmp_path):
    store = GoalWaits(tmp_path / "goal_waits.json")
    store.record(wait())

    def fail(rows):
        raise RuntimeError("change failed")

    with pytest.raises(RuntimeError, match="change failed"):
        store.update(fail)
    with pytest.raises(ValueError, match="owner incarnation"):
        store.record(replace(wait(), owner_created_at=float("nan")))
    # The failed callback/encoder released the lock.
    store.record(replace(wait(), revision=3))


def test_invalid_boundary_is_not_overwritten(tmp_path):
    store = GoalWaits(tmp_path / "goal_waits.json")
    data = FieldCodec.encode({"goal": wait()})
    data["goal"]["revision"] = True
    text = json.dumps(data)
    store.path.write_text(text)
    with pytest.raises(ValueError, match="Expected"):
        store.record(wait())
    assert store.path.read_text() == text


@pytest.mark.skipif(os.name != "posix", reason="POSIX flock contention contract")
def test_shared_readers_overlap_and_exclude_writer(tmp_path):
    path = tmp_path / "goal_waits.json"
    GoalWaits(path).record(wait())
    ready1, ready2, release = event(), event(), event()
    with (
        process(_hold_lock, path, True, ready1, release),
        process(_hold_lock, path, True, ready2, release),
    ):
        try:
            assert ready1.wait(5) and ready2.wait(5)
            started, done = event(), event()
            with process(_read, path, started, done):
                assert done.wait(5), "snapshot did not share the held read locks"
            with pytest.raises(BlockingIOError), _store_lock(path, blocking=False):
                pass
        finally:
            release.set()


@pytest.mark.skipif(os.name != "posix", reason="POSIX flock contention contract")
def test_exclusive_writer_blocks_snapshot_and_other_writer(tmp_path):
    path = tmp_path / "goal_waits.json"
    ready, release, started, done = event(), event(), event(), event()
    with process(_hold_lock, path, False, ready, release):
        try:
            assert ready.wait(5)
            with process(_read, path, started, done):
                assert started.wait(5)
                assert not done.wait(0.2)
                with pytest.raises(BlockingIOError), _store_lock(path, blocking=False):
                    pass
                release.set()
                assert done.wait(5)
        finally:
            release.set()


def test_concurrent_writers_keep_all_rows(tmp_path):
    path, start = tmp_path / "goal_waits.json", event()
    with process(_record, path, start, "first"), process(_record, path, start, "second"):
        start.set()
    assert set(GoalWaits(path).read()) == {
        f"{prefix}-{index}" for prefix in ("first", "second") for index in range(12)
    }


def test_wire_nesting_does_not_reacquire_goal_lock(tmp_path):
    done = event()
    with process(_record_under_wire, tmp_path / "goal_waits.json", done):
        assert done.wait(5)


def test_pause_store_golden_and_shared_algorithm(tmp_path):
    store = GoalPauseEvents(tmp_path / "goal_pause_events.json")
    assert GoalPauseEvents.update is LockedStore.update
    assert store.read() == {}
    with _store_lock(tmp_path / "wire"):
        store.record(GoalPauseEvent("goal", 3, OwnerPause()))
        store.record(GoalPauseEvent("goal", 4, RuntimePause()))
        rows = store.read()
    assert store.path.read_text() == (
        '{"goal:3": {"goal_id": "goal", "revision": 3, "source": "owner"}, '
        '"goal:4": {"goal_id": "goal", "revision": 4, "source": "runtime"}}'
    )
    assert rows["goal:3"].source == OwnerPause()
    assert GoalPauseEvents.for_goal(Goal("Work", "goal", revision=3, state=PausedGoal()))
    assert (
        GoalPauseEvents.for_goal(Goal("Work", "goal", revision=5, state=PausedGoal())).source
        == OwnerPause()
    )


def test_history_codec_roundtrips_current_goal_and_rejects_invalid_revision():
    goal = Goal("Work", "goal")
    assert GoalHistoryStore._decode(GoalHistoryStore._encode(goal)) == goal
    assert GoalHistoryStore._encode(None) is None
    assert GoalHistoryStore._decode(None) is None
    with pytest.raises(GoalHistoryError, match="invalid goal snapshot"):
        GoalHistoryStore._decode('{"text": "Work", "id": "goal", "revision": true}')
