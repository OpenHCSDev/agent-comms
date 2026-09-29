"""Current S4 Part B: authority lifetime and disposable-index recovery."""

from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.threads import Thread
from agent_comms.view_unread import TranscriptReadState
from test_thread_unread import append


def test_replaced_human_incarnation_rejects_painted_proof_and_prior_reads(tmp_path):
    comms = wire(tmp_path)
    comms.threads.register(Thread("worker", frozenset({"team"}), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    message = comms.messaging.send_message("worker", "#team", "painted")
    page = comms.views.channel_display_page("#team", worktree=str(tmp_path))
    comms.views.mark_channel_view_read(
        "#team", worktree=str(tmp_path), through=page.newest_seq,
        expected_scope=page.display_scope,
    )
    assert message.seq in comms.bus.reads.seen_sequences(viewer, comms.registry.snapshot())
    original = comms.registry.require(viewer)
    comms.registry.unregister(viewer)
    comms.registry.remove(viewer)
    comms.registry.register(replace(original, created_at=original.created_at + 1))
    assert message.seq not in comms.bus.reads.seen_sequences(viewer, comms.registry.snapshot())
    with pytest.raises(ValueError):
        comms.views.mark_channel_view_read(
            "#team", worktree=str(tmp_path), through=page.newest_seq,
            expected_scope=page.display_scope,
        )
    refreshed = comms.views.channel_display_page("#team", worktree=str(tmp_path))
    comms.views.mark_channel_view_read(
        "#team", worktree=str(tmp_path), through=refreshed.newest_seq,
        expected_scope=refreshed.display_scope,
    )
    assert message.seq in comms.bus.reads.seen_sequences(viewer, comms.registry.snapshot())


def test_index_rebuild_preserves_distinct_transcript_bus_and_delivery_facts(tmp_path):
    comms = wire(tmp_path / "wire")
    source = tmp_path / "native.jsonl"
    append(source, content="painted reply")
    comms.threads.register(Thread("worker", frozenset({"team"}), str(tmp_path),
                                 session_file=str(source)))
    comms.threads.register(Thread("receiver", frozenset({"team"}), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    shown = comms.transcripts.transcript_checkpoint("worker")
    append(source, content="unpainted reply")
    message = comms.messaging.send_message("worker", "#team", "unpainted bus row")
    page = comms.views.channel_display_page("#team", worktree=str(tmp_path))
    assert message.seq not in comms.bus.reads.seen_sequences(viewer, comms.registry.snapshot())
    comms.views.mark_thread_view_read("worker", worktree=str(tmp_path), through=shown)
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 1
    document = comms.bus.reads.read()
    assert document.transcripts and not document.messages
    index_owner = comms.views.transcript_reads
    index_owner.close()
    index_owner._index_path.unlink()
    rebuilt = TranscriptReadState(comms.bus.reads.path)
    try:
        assert rebuilt.counts(viewer, {"worker": str(source)}).counts == {"worker": 1}
        assert comms.bus.reads.read() == document
        assert comms.bus.pending_count("receiver", "#team") == 1
        comms.views.mark_channel_view_read(
            "#team", worktree=str(tmp_path), through=page.newest_seq,
            expected_scope=page.display_scope,
        )
        assert rebuilt.counts(viewer, {"worker": str(source)}).counts == {"worker": 1}
        assert comms.bus.pending_count("receiver", "#team") == 1
        assert comms.bus.reads.read().transcripts == document.transcripts
        comms.messaging.acknowledge("receiver")
        assert comms.bus.pending_count("receiver", "#team") == 0
        assert rebuilt.counts(viewer, {"worker": str(source)}).counts == {"worker": 1}
    finally:
        rebuilt.close()
