"""Append-aware activity snapshots preserve the existing JSONL recovery contract."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from agent_comms.activity import Activity, ActivityLog, ActivityState
from agent_comms.field_codec import FieldCodec
from agent_comms.store_files import _atomic_write_text
from agent_comms.threads import Thread


def encoded(name="a", detail="before"):
    return json.dumps(FieldCodec.encode(Activity(name, ActivityState.WORKING, detail)))


def test_appends_parse_only_new_records_and_do_not_mutate_published_snapshots(tmp_path):
    path = tmp_path / "activity.jsonl"
    path.write_text("".join(encoded(f"worker-{index % 10}") + "\n" for index in range(3000)))
    reader, writer = ActivityLog(path), ActivityLog(path)
    previous = reader._latest_events()
    with patch.object(FieldCodec, "decode", wraps=FieldCodec.decode) as parse:
        for _ in range(10):
            reader.all_current()
        assert sum(call.args[0] is Activity for call in parse.call_args_list) == 0
        writer.emit(Activity("new-worker", ActivityState.THINKING, "latest"))
        current = reader.all_current()
        assert current["new-worker"].detail == "latest"
        assert sum(call.args[0] is Activity for call in parse.call_args_list) == 1
    assert "new-worker" not in previous


def test_fresh_reader_preserves_latest_threads_and_sees_append(tmp_path):
    path = tmp_path / "activity.jsonl"
    path.write_text("".join(encoded(f"worker-{index % 10}") + "\n" for index in range(3000)))
    previous = ActivityLog(path).all_current()
    assert set(previous) == {f"worker-{index}" for index in range(10)}
    assert ActivityLog(path).all_current() == previous
    appended = Activity("new-worker", ActivityState.THINKING, "latest")
    ActivityLog(path).emit(appended)
    assert ActivityLog(path).all_current() == {**previous, "new-worker": appended}


def test_competing_readers_do_not_return_an_older_published_cut(tmp_path, monkeypatch):
    from agent_comms.activity_checkpoint import ActivityCheckpoint

    path = tmp_path / "activity.jsonl"
    log = ActivityLog(path)
    log.emit(Activity("a", ActivityState.THINKING))
    log.all_current()
    # External appends still enter through the read path. Canonical writers
    # now fold and persist their own checkpoint before returning.
    with path.open("a") as stream:
        stream.write(encoded("b") + "\n")
    earlier_size = path.stat().st_size
    earlier_ready, later_ready = threading.Event(), threading.Event()
    publish_earlier, publish_later = threading.Event(), threading.Event()
    capture = ActivityCheckpoint.capture
    later_size = None

    def suspended_capture(cls, stream, revision, latest, offset):
        if revision.size == earlier_size:
            earlier_ready.set()
            assert publish_earlier.wait(5)
        elif revision.size == later_size:
            later_ready.set()
            assert publish_later.wait(5)
        return capture(stream, revision, latest, offset)

    monkeypatch.setattr(ActivityCheckpoint, "capture", classmethod(suspended_capture))
    with ThreadPoolExecutor(max_workers=2) as readers:
        earlier = readers.submit(log.all_current)
        assert earlier_ready.wait(5)
        with path.open("a") as stream:
            stream.write(encoded("c") + "\n")
        later_size = path.stat().st_size
        later = readers.submit(log.all_current)
        try:
            assert later_ready.wait(5)
            publish_earlier.set()
            assert set(earlier.result(5)) == {"a", "b"}
            publish_later.set()
            assert set(later.result(5)) == {"a", "b", "c"}
        finally:
            publish_earlier.set()
            publish_later.set()


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


def test_replacement_truncation_rename_delete_and_missing_file_reset_index(tmp_path):
    path = tmp_path / "activity.jsonl"
    reader, writer = ActivityLog(path), ActivityLog(path)
    writer.emit(Activity("a", ActivityState.THINKING, "old"))
    writer.emit(Activity("b", ActivityState.THINKING, "old"))
    assert set(reader.all_current()) == {"a", "b"}
    writer.rename_thread("a", "renamed")
    assert set(reader.all_current()) == {"renamed", "b"}
    writer.remove_threads((Thread("b", frozenset(), str(tmp_path)),))
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
    # This is the real UI failure: a warm observer must not publish its read
    # and wait behind another process writing the disposable document.
    with ThreadPoolExecutor(max_workers=1) as readers:
        with log.checkpoint.locked():
            assert set(readers.submit(log.all_current).result(2)) == {"a", "b"}
    assert set(log.checkpoint.read().latest) == {"a"}


def test_source_writers_publish_checkpoint_and_noop_updates_do_not(tmp_path):
    from agent_comms.activity_checkpoint import ActivityCheckpointStore
    from agent_comms.private_path import FileRevision

    log = ActivityLog(tmp_path / "activity.jsonl")
    log.emit(Activity("a", ActivityState.THINKING))
    checkpoint = log.checkpoint.read()
    assert checkpoint.source == FileRevision.capture(log._path)
    assert set(checkpoint.latest) == {"a"}
    assert log.update("b", lambda _: Activity("b", ActivityState.WORKING))
    assert set(log.checkpoint.read().latest) == {"a", "b"}
    log.rename_thread("a", "renamed")
    checkpoint = log.checkpoint.read()
    assert checkpoint.source == FileRevision.capture(log._path)
    assert set(checkpoint.latest) == {"renamed", "b"}
    with patch.object(ActivityCheckpointStore, "replace") as publish:
        assert not log.update("b", lambda _: None)
        publish.assert_not_called()


def test_cached_activity_still_expires_unless_its_thread_is_active(tmp_path):
    log = ActivityLog(tmp_path / "activity.jsonl", stale_after=10)
    log.emit(Activity("a", ActivityState.THINKING, "working", timestamp=100))
    with patch("agent_comms.store_files.time.time", return_value=105):
        assert log.current("a").state is ActivityState.THINKING
    with patch("agent_comms.store_files.time.time", return_value=111):
        assert log.current("a").state is ActivityState.IDLE
        assert log.all_current(active=frozenset({"a"}))["a"].state is ActivityState.THINKING
