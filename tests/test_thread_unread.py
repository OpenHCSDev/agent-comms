import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from agent_comms.comms import wire
from agent_comms.native_entries import NativeEntry
from agent_comms.threads import Thread
from agent_comms.transcripts import TranscriptCursor
from agent_comms.view_unread import TranscriptReadState


def append(path, role="assistant", content="Reply"):
    with path.open("a") as stream:
        stream.write(
            json.dumps({"type": "message", "message": {"role": role, "content": content}}) + "\n"
        )


def setup_thread(tmp_path):
    source = tmp_path / "session.jsonl"
    source.touch()
    comms = wire(tmp_path / "wire")
    comms.threads.register(
        Thread("worker", frozenset({"ci"}), str(tmp_path), session_file=str(source))
    )
    return comms, source


def test_reply_counts_are_persistent_human_read_positions(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source, "user", "My own prompt")
    append(source, content=[{"type": "thinking", "thinking": "private"}])
    append(source, content=[{"type": "toolCall", "id": "tool", "name": "read"}])
    append(source, "toolResult", "tool output")
    append(source, content=[{"type": "text", "text": "one"}, {"type": "text", "text": "two"}])
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 1
    shown = comms.transcripts.transcript_checkpoint("worker")
    append(source, content="Arrived after the displayed checkpoint")
    comms.views.mark_thread_view_read("worker", worktree=str(tmp_path), through=shown)
    assert wire(comms.root).views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 1
    # The wire's durable human identity shares read positions across projects.
    assert comms.views.viewer_snapshot(str(tmp_path / "other-project")).thread_unread["worker"] == 1
    comms.messaging.send("worker", "#ci", "Channel reply")
    comms.views.mark_channel_view_read("#ci", worktree=str(tmp_path))
    comms.messaging.acknowledge("worker")
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 1


def test_idle_observation_does_not_reparse_transcripts_and_append_is_incremental(tmp_path):
    comms, source = setup_thread(tmp_path)
    for _ in range(50):
        append(source)
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 50
    with patch.object(NativeEntry, "read", wraps=NativeEntry.read) as parse:
        comms.views.viewer_snapshot(str(tmp_path))
        assert parse.call_count == 0
        append(source)
        assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 51
        assert parse.call_count == 1


def test_new_process_uses_reply_index_without_reparsing_history(tmp_path):
    comms, source = setup_thread(tmp_path)
    for _ in range(100):
        append(source)
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 100
    fresh = TranscriptReadState(comms.views.transcript_reads.path)
    sources = {"worker": str(source)}
    with patch.object(NativeEntry, "read", wraps=NativeEntry.read) as parse:
        assert fresh.counts(str(tmp_path), sources).counts["worker"] == 100
        assert parse.call_count == 0
        append(source)
        assert fresh.counts(str(tmp_path), sources).counts["worker"] == 101
        assert parse.call_count == 1


def test_many_threads_do_not_reparse_saved_histories_on_attach(tmp_path):
    comms, _ = setup_thread(tmp_path)
    sources = {}
    for number in range(80):
        source = tmp_path / f"session-{number}.jsonl"
        for _ in range(20):
            append(source)
        sources[f"thread-{number}"] = str(source)
    first = TranscriptReadState(comms.views.transcript_reads.path)
    for _ in range(20):
        observed = first.counts("viewer", sources)
        if not observed.pending:
            break
    assert not observed.pending
    assert set(observed.counts.values()) == {20}
    first.close()
    fresh = TranscriptReadState(comms.views.transcript_reads.path)
    with patch.object(NativeEntry, "read", wraps=NativeEntry.read) as parse:
        assert set(fresh.counts("viewer", sources).counts.values()) == {20}
        assert parse.call_count == 0


def test_reply_index_rebuilds_after_source_replacement(tmp_path):
    comms, source = setup_thread(tmp_path)
    for _ in range(30):
        append(source)
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 30
    replacement = tmp_path / "new-session.jsonl"
    append(replacement)
    replacement.replace(source)
    fresh = TranscriptReadState(comms.views.transcript_reads.path)
    assert fresh.counts(str(tmp_path), {"worker": str(source)}).counts == {"worker": 1}


def test_corrupt_derived_index_rebuilds_from_transcript(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 1
    index = comms.views.transcript_reads._index_path
    comms.views.transcript_reads.close()
    index.write_bytes(b"damaged cache")
    fresh = TranscriptReadState(comms.views.transcript_reads.path)
    assert fresh.counts(str(tmp_path), {"worker": str(source)}).counts == {"worker": 1}


def test_open_views_share_one_incremental_index_without_sharing_markers(tmp_path):
    comms, source = setup_thread(tmp_path)
    for _ in range(30):
        append(source)
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 30
    second = wire(comms.root)
    assert second.views.transcript_reads is comms.views.transcript_reads
    with patch.object(NativeEntry, "read", wraps=NativeEntry.read) as parse:
        assert second.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 30
        assert parse.call_count == 0
        append(source)
        assert second.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 31
        assert parse.call_count == 1
    checkpoint = second.transcripts.transcript_checkpoint("worker")
    second.views.mark_thread_view_read("worker", worktree=str(tmp_path), through=checkpoint)
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 0


def test_partial_tail_and_replaced_source_do_not_lose_replies(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    comms.views.mark_thread_view_read(
        "worker", worktree=str(tmp_path), through=comms.transcripts.transcript_checkpoint("worker")
    )
    with source.open("a") as stream:
        stream.write('{"type":"message","message":{"role":"assistant","content":"next')
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 0
    with source.open("a") as stream:
        stream.write('"}}\n')
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 1
    replacement = tmp_path / "replacement.jsonl"
    append(replacement)
    replacement.replace(source)
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 1


def test_rename_preserves_reads_and_fresh_fork_does_not_count_parent_history(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    comms.views.mark_thread_view_read(
        "worker", worktree=str(tmp_path), through=comms.transcripts.transcript_checkpoint("worker")
    )
    comms.registry.rename("worker", "renamed")
    comms.threads.register(
        Thread("child", frozenset(), str(tmp_path), parent="renamed", task="New task")
    )
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread == {"renamed": 0, "child": 0}
    append(source)
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread == {"renamed": 1, "child": 0}


def test_old_or_invalid_checkpoint_does_not_acknowledge_new_source(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    stale = comms.transcripts.transcript_checkpoint("worker")
    other = tmp_path / "other.jsonl"
    append(other)
    comms.registry.register(replace(comms.registry.require("worker"), session_file=str(other)))
    with pytest.raises(ValueError, match="Transcript changed"):
        comms.views.mark_thread_view_read("worker", worktree=str(tmp_path), through=stale)
    with pytest.raises(ValueError, match="Transcript changed"):
        comms.views.mark_thread_view_read(
            "worker",
            worktree=str(tmp_path),
            through=TranscriptCursor(str(other), other.stat().st_size + 1),
        )
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 1


def test_bounded_batches_resume_after_process_exit_and_preserve_read_cursor(tmp_path):
    source = tmp_path / "large.jsonl"
    for _ in range(1200):
        append(source)
    path = tmp_path / "read_ledger.json"
    first = TranscriptReadState(path)
    first.mark_read("viewer", str(source), source.stat().st_size)
    sources = {"worker": str(source)}
    partial = first.counts("viewer", sources)
    assert partial.pending == {"worker"}
    assert "worker" not in partial.counts  # No exact zero while the count is unknown.
    first.close()
    fresh = TranscriptReadState(path)
    for _ in range(10):
        observed = fresh.counts("viewer", sources)
        if not observed.pending:
            break
    assert not observed.pending
    assert observed.counts == {"worker": 0}
    append(source)
    assert fresh.counts("viewer", sources).counts == {"worker": 1}
    fresh.close()


def test_large_first_source_does_not_starve_other_sources(tmp_path):
    sources = {}
    for number in range(8):
        source = tmp_path / f"large-{number}.jsonl"
        for _ in range(600):
            append(source)
        sources[str(number)] = str(source)
    state = TranscriptReadState(tmp_path / "read_ledger.json")
    completed = set()
    for _ in range(40):
        observed = state.counts("viewer", sources)
        completed.update(observed.counts)
        if not observed.pending:
            break
    assert completed == set(sources)
    assert not observed.pending
    assert set(observed.counts.values()) == {600}
    state.close()


def test_close_cancels_executor_without_scanning_rest_of_native_file(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from time import monotonic

    source = tmp_path / "large.jsonl"
    for _ in range(5000):
        append(source, content="real native record " * 100)
    state = TranscriptReadState(tmp_path / "read_ledger.json")
    started = monotonic()
    with ThreadPoolExecutor(max_workers=1) as executor:
        task = executor.submit(state.counts, "viewer", {"worker": str(source)})
        state.close()
        observed = task.result(timeout=2)
        assert observed.pending == {"worker"}
    assert monotonic() - started < 2
    assert state.counts("viewer", {"worker": str(source)}).pending == {"worker"}


def test_busy_index_is_not_discarded(tmp_path):
    import sqlite3

    source = tmp_path / "source.jsonl"
    append(source)
    state = TranscriptReadState(tmp_path / "read_ledger.json")
    assert state.counts("viewer", {"worker": str(source)}).counts == {"worker": 1}
    inode = state._index_path.stat().st_ino
    append(source)
    blocker = sqlite3.connect(state._index_path)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            state.counts("viewer", {"worker": str(source)})
        assert state._index_path.stat().st_ino == inode
    finally:
        blocker.rollback()
        blocker.close()
    assert state.counts("viewer", {"worker": str(source)}).counts == {"worker": 2}
    state.close()
