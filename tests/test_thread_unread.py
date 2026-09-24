import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from agent_comms import Thread, TranscriptCursor, wire
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
    comms.register(Thread("worker", frozenset({"ci"}), str(tmp_path), session_file=str(source)))
    return comms, source


def test_reply_counts_are_persistent_human_read_positions(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source, "user", "My own prompt")
    append(source, content=[{"type": "thinking", "thinking": "private"}])
    append(source, content=[{"type": "toolCall", "id": "tool", "name": "read"}])
    append(source, "toolResult", "tool output")
    append(source, content=[{"type": "text", "text": "one"}, {"type": "text", "text": "two"}])
    assert comms.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 1
    shown = comms.transcript_checkpoint("worker")
    append(source, content="Arrived after the displayed checkpoint")
    comms.mark_thread_view_read("worker", worktree=str(tmp_path), through=shown)
    assert wire(comms.root).viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 1
    # The wire's durable human identity shares read positions across projects.
    assert comms.viewer_snapshot(str(tmp_path / "other-project")).thread_unread["worker"] == 1
    comms.send("worker", "#ci", "Channel reply")
    comms.mark_channel_view_read("#ci", worktree=str(tmp_path))
    comms.acknowledge("worker")
    assert comms.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 1


def test_idle_observation_does_not_reparse_transcripts_and_append_is_incremental(tmp_path):
    comms, source = setup_thread(tmp_path)
    for _ in range(50):
        append(source)
    assert comms.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 50
    with patch.object(comms, "_is_unread_reply", wraps=comms._is_unread_reply) as parse:
        comms.viewer_snapshot(str(tmp_path))
        assert parse.call_count == 0
        append(source)
        assert comms.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 51
        assert parse.call_count == 1


def test_new_process_uses_reply_index_without_reparsing_history(tmp_path):
    comms, source = setup_thread(tmp_path)
    for _ in range(100):
        append(source)
    assert comms.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 100
    fresh = TranscriptReadState(comms.transcript_reads.path)
    sources = {"worker": str(source)}
    with patch.object(comms, "_is_unread_reply", wraps=comms._is_unread_reply) as parse:
        assert fresh.counts(str(tmp_path), sources, comms._is_unread_reply)["worker"] == 100
        assert parse.call_count == 0
        append(source)
        assert fresh.counts(str(tmp_path), sources, comms._is_unread_reply)["worker"] == 101
        assert parse.call_count == 1


def test_many_threads_do_not_reparse_saved_histories_on_attach(tmp_path):
    comms, _ = setup_thread(tmp_path)
    sources = {}
    for number in range(80):
        source = tmp_path / f"session-{number}.jsonl"
        for _ in range(20):
            append(source)
        sources[f"thread-{number}"] = str(source)
    first = TranscriptReadState(comms.transcript_reads.path)
    assert set(first.counts("viewer", sources, comms._is_unread_reply).values()) == {20}
    first.close()
    fresh = TranscriptReadState(comms.transcript_reads.path)
    with patch.object(comms, "_is_unread_reply", wraps=comms._is_unread_reply) as parse:
        assert set(fresh.counts("viewer", sources, comms._is_unread_reply).values()) == {20}
        assert parse.call_count == 0


def test_reply_index_rebuilds_after_source_replacement(tmp_path):
    comms, source = setup_thread(tmp_path)
    for _ in range(30):
        append(source)
    assert comms.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 30
    replacement = tmp_path / "new-session.jsonl"
    append(replacement)
    replacement.replace(source)
    fresh = TranscriptReadState(comms.transcript_reads.path)
    assert fresh.counts(str(tmp_path), {"worker": str(source)}, comms._is_unread_reply) == {
        "worker": 1
    }


def test_corrupt_derived_index_rebuilds_from_transcript(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    assert comms.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 1
    index = comms.transcript_reads._index_path
    comms.transcript_reads.close()
    index.write_bytes(b"damaged cache")
    fresh = TranscriptReadState(comms.transcript_reads.path)
    assert fresh.counts(str(tmp_path), {"worker": str(source)}, comms._is_unread_reply) == {
        "worker": 1
    }


def test_open_views_share_one_incremental_index_without_sharing_markers(tmp_path):
    comms, source = setup_thread(tmp_path)
    for _ in range(30):
        append(source)
    assert comms.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 30
    second = wire(comms.root)
    assert second.transcript_reads is comms.transcript_reads
    with patch.object(second, "_is_unread_reply", wraps=second._is_unread_reply) as parse:
        assert second.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 30
        assert parse.call_count == 0
        append(source)
        assert second.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 31
        assert parse.call_count == 1
    checkpoint = second.transcript_checkpoint("worker")
    second.mark_thread_view_read("worker", worktree=str(tmp_path), through=checkpoint)
    assert comms.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 0


def test_partial_tail_and_replaced_source_do_not_lose_replies(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    comms.mark_thread_view_read(
        "worker", worktree=str(tmp_path), through=comms.transcript_checkpoint("worker")
    )
    with source.open("a") as stream:
        stream.write('{"type":"message","message":{"role":"assistant","content":"next')
    assert comms.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 0
    with source.open("a") as stream:
        stream.write('"}}\n')
    assert comms.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 1
    replacement = tmp_path / "replacement.jsonl"
    append(replacement)
    replacement.replace(source)
    assert comms.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 1


def test_rename_preserves_reads_and_fresh_fork_does_not_count_parent_history(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    comms.mark_thread_view_read(
        "worker", worktree=str(tmp_path), through=comms.transcript_checkpoint("worker")
    )
    comms.registry.rename("worker", "renamed")
    comms.register(Thread("child", frozenset(), str(tmp_path), parent="renamed", task="New task"))
    assert comms.viewer_snapshot(str(tmp_path)).thread_unread == {"renamed": 0, "child": 0}
    append(source)
    assert comms.viewer_snapshot(str(tmp_path)).thread_unread == {"renamed": 1, "child": 0}


def test_old_or_invalid_checkpoint_does_not_acknowledge_new_source(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    stale = comms.transcript_checkpoint("worker")
    other = tmp_path / "other.jsonl"
    append(other)
    comms.registry.register(replace(comms.registry.require("worker"), session_file=str(other)))
    with pytest.raises(ValueError, match="Transcript changed"):
        comms.mark_thread_view_read("worker", worktree=str(tmp_path), through=stale)
    with pytest.raises(ValueError, match="Transcript changed"):
        comms.mark_thread_view_read(
            "worker",
            worktree=str(tmp_path),
            through=TranscriptCursor(str(other), other.stat().st_size + 1),
        )
    assert comms.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 1
