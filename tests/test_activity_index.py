"""Append-aware activity snapshots preserve the existing JSONL recovery contract."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_comms.activity import Activity, ActivityLog, ActivityState
from agent_comms.field_codec import FieldCodec
from agent_comms.store_files import _atomic_write_text


def encoded(name="a", detail="before"):
    return json.dumps(FieldCodec.encode(Activity(name, ActivityState.WORKING, detail)))


def test_fresh_reader_preserves_latest_threads_and_sees_append(tmp_path):
    path = tmp_path / "activity.jsonl"
    path.write_text("".join(encoded(f"worker-{index % 10}") + "\n" for index in range(3000)))
    previous = ActivityLog(path).all_current()
    assert set(previous) == {f"worker-{index}" for index in range(10)}
    assert ActivityLog(path).all_current() == previous
    appended = Activity("new-worker", ActivityState.THINKING, "latest")
    ActivityLog(path).emit(appended)
    assert ActivityLog(path).all_current() == {**previous, "new-worker": appended}


def test_fresh_reader_rebuilds_after_activity_rewrite_before_append(tmp_path):
    path = tmp_path / "activity.jsonl"
    path.write_text(encoded("a") + "\n")
    assert set(ActivityLog(path).all_current()) == {"a"}
    path.write_text(encoded("b") + "\n")
    ActivityLog(path).emit(Activity("c", ActivityState.THINKING, "new"))
    assert set(ActivityLog(path).all_current()) == {"b", "c"}


def test_partial_and_valid_unterminated_tails_are_retried(tmp_path):
    path = tmp_path / "activity.jsonl"
    path.write_text(encoded(detail="before") + "\n")
    reader = ActivityLog(path)
    assert reader.current("a").detail == "before"
    tail = encoded(detail="after")
    with path.open("a") as stream:
        stream.write(tail[:20])
    assert reader.current("a").detail == "before"
    with path.open("a") as stream:
        stream.write(tail[20:])
    assert reader.current("a").detail == "after"
    # An uncommitted EOF record must not contaminate the complete-prefix cache.
    with path.open("a") as stream:
        stream.write(" invalid")
    assert reader.current("a").detail == "before"
    ActivityLog(path).emit(Activity("a", ActivityState.THINKING, "recovered"))
    assert reader.current("a").detail == "recovered"
    assert path.with_name("activity.jsonl.corrupt").exists()


def test_blank_lines_and_valid_tail_completion_keep_exact_cursor(tmp_path):
    path = tmp_path / "activity.jsonl"
    path.write_text("\n  \n" + encoded("a") + "\n\n" + encoded("b"))
    reader = ActivityLog(path)
    assert set(reader.all_current()) == {"a", "b"}
    ActivityLog(path).emit(Activity("c", ActivityState.THINKING, "new"))
    assert set(reader.all_current()) == {"a", "b", "c"}


def test_replacement_truncation_rename_delete_and_missing_file_reset_index(tmp_path, *, thread_registration):
    path = tmp_path / "activity.jsonl"
    reader, writer = ActivityLog(path), ActivityLog(path)
    writer.emit(Activity("a", ActivityState.THINKING, "old"))
    writer.emit(Activity("b", ActivityState.THINKING, "old"))
    assert set(reader.all_current()) == {"a", "b"}
    writer.rename_thread("a", "renamed")
    assert set(reader.all_current()) == {"renamed", "b"}
    writer.remove_threads((thread_registration.new_thread('b', frozenset(), str(tmp_path)),))
    assert set(reader.all_current()) == {"renamed"}
    path.write_text(encoded("c") + "\n")
    assert set(reader.all_current()) == {"c"}
    _atomic_write_text(path, encoded("d") + "\n")
    assert set(reader.all_current()) == {"d"}
    path.unlink()
    assert reader.all_current() == {}


def test_malformed_complete_record_never_returns_stale_success(tmp_path):
    path = tmp_path / "activity.jsonl"
    path.write_text(encoded() + "\n")
    reader = ActivityLog(path)
    reader.all_current()
    with path.open("a") as stream:
        stream.write("{bad json}\n")
    for _ in range(2):
        with pytest.raises(json.JSONDecodeError):
            reader.all_current()
    before = path.read_bytes()
    with pytest.raises(json.JSONDecodeError):
        reader.emit(Activity("new", ActivityState.THINKING))
    assert path.read_bytes() == before
    _atomic_write_text(path, encoded("fixed") + "\n")
    assert set(reader.all_current()) == {"fixed"}


def test_reading_new_events_does_not_wait_for_checkpoint_writer(tmp_path):
    path = tmp_path / "activity.jsonl"
    log = ActivityLog(path)
    log.emit(Activity("a", ActivityState.THINKING))
    with path.open("a") as stream:
        stream.write(encoded("b") + "\n")
    # A cold observer must neither lock nor publish the disposable document.
    from agent_comms.activity import ActivityReadState
    from agent_comms.store_files import _store_lock
    log._read = ActivityReadState()
    with ThreadPoolExecutor(max_workers=1) as readers:
        with _store_lock(log.checkpoint.path):
            assert set(readers.submit(log.all_current).result(2)) == {"a", "b"}
    assert set(log.checkpoint.read().latest) == {"a"}


def test_failed_checkpoint_reports_error_without_erasing_the_committed_event(tmp_path):
    path = tmp_path / "activity.jsonl"
    log = ActivityLog(path)
    log.checkpoint.path.mkdir()
    event = Activity("a", ActivityState.WORKING, "already recorded")
    with pytest.raises(OSError):
        log.emit(event)
    assert log.current("a", active=True) == event
    assert [Activity.from_wire(json.loads(line)) for line in path.read_text().splitlines()] == [event]
