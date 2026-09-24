"""Append-aware activity snapshots preserve the existing JSONL recovery contract."""

import json
from unittest.mock import patch

import pytest

from agent_comms.declarations import Activity, ActivityLog, ActivityState, _atomic_write_text


def encoded(name="a", detail="before"):
    return json.dumps(Activity(name, ActivityState.WORKING, detail).to_wire())


def test_appends_parse_only_new_records_and_do_not_mutate_published_snapshots(tmp_path):
    path = tmp_path / "activity.jsonl"
    path.write_text("".join(encoded(f"worker-{index % 10}") + "\n" for index in range(3000)))
    reader, writer = ActivityLog(path), ActivityLog(path)
    previous = reader._latest_events()
    with patch.object(Activity, "from_wire", wraps=Activity.from_wire) as parse:
        for _ in range(10):
            reader.all_current()
        assert parse.call_count == 0
        writer.emit(Activity("new-worker", ActivityState.THINKING, "latest"))
        current = reader.all_current()
        assert current["new-worker"].detail == "latest"
        assert parse.call_count == 1
    assert "new-worker" not in previous


def test_fresh_reader_loads_latest_threads_without_replaying_activity_history(tmp_path):
    path = tmp_path / "activity.jsonl"
    path.write_text("".join(encoded(f"worker-{index % 10}") + "\n" for index in range(3000)))
    assert len(ActivityLog(path).all_current()) == 10
    with patch.object(Activity, "from_wire", wraps=Activity.from_wire) as parse:
        assert len(ActivityLog(path).all_current()) == 10
        assert parse.call_count == 10
    ActivityLog(path).emit(Activity("new-worker", ActivityState.THINKING, "latest"))
    with patch.object(Activity, "from_wire", wraps=Activity.from_wire) as parse:
        current = ActivityLog(path).all_current()
        assert current["new-worker"].detail == "latest"
        assert parse.call_count == 11


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
    writer.remove_thread("b")
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
    _atomic_write_text(path, encoded("fixed") + "\n")
    assert set(reader.all_current()) == {"fixed"}


def test_cached_activity_still_expires_unless_its_thread_is_active(tmp_path):
    log = ActivityLog(tmp_path / "activity.jsonl", stale_after=10)
    log.emit(Activity("a", ActivityState.THINKING, "working", timestamp=100))
    with patch("agent_comms.declarations.time.time", return_value=105):
        assert log.current("a").state is ActivityState.THINKING
    with patch("agent_comms.declarations.time.time", return_value=111):
        assert log.current("a").state is ActivityState.IDLE
        assert log.all_current(active=frozenset({"a"}))["a"].state is ActivityState.THINKING
