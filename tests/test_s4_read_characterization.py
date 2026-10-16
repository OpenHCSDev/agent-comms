"""Current S4 Part B: authority lifetime and disposable-index recovery."""

from native_unread_fixture import counts, transcript_fixture
from tests.read_membership import stored_read_state
from tests.read_membership import read_mask
from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.threads import Thread

from native_unread_fixture import completed_counts, completed_snapshot
from test_thread_unread import append


def test_replaced_human_incarnation_rejects_painted_proof_and_prior_reads(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("worker", frozenset({"team"}), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    message = comms.messaging.send_message("worker", "#team", "painted")
    page = comms.views.channel_display_page("#team", worktree=str(tmp_path))
    comms.views.mark_channel_view_read(
        "#team", worktree=str(tmp_path), through=page.newest_seq,
        expected_scope=page.display_scope,
    )
    assert read_mask(comms, viewer, comms.registry.snapshot()).contains(message)
    original = comms.registry.require(viewer)
    comms.registry.unregister(viewer)
    comms.registry.remove(viewer)
    comms.registry.register(replace(original, created_at=original.created_at + 1))
    assert not read_mask(comms, viewer, comms.registry.snapshot()).contains(message)
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
    assert read_mask(comms, viewer, comms.registry.snapshot()).contains(message)


def test_index_rebuild_preserves_distinct_transcript_bus_and_delivery_facts(tmp_path):
    comms = wire(tmp_path / "wire")
    source = tmp_path / "native.jsonl"
    append(source, content="painted reply")
    comms.registry.declare(Thread("worker", frozenset({"team"}), str(tmp_path),
                                 session_file=str(source)))
    comms.registry.declare(Thread("receiver", frozenset({"team"}), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    shown = comms.transcripts.transcript_checkpoint("worker")
    append(source, content="unpainted reply")
    message = comms.messaging.send_message("worker", "#team", "unpainted bus row")
    page = comms.views.channel_display_page("#team", worktree=str(tmp_path))
    assert not read_mask(comms, viewer, comms.registry.snapshot()).contains(message)
    comms.views.mark_thread_view_read("worker", worktree=str(tmp_path), through=shown)
    assert completed_snapshot(comms, str(tmp_path)).thread_unread["worker"] == 1
    document = stored_read_state(comms.bus.reads)
    assert document.transcripts and not document.messages
    index_owner = comms.views.transcript_reads
    index_owner.close()
    index_owner.index_path.unlink()
    rebuilt = transcript_fixture(comms.bus.reads.path)
    try:
        assert completed_counts(rebuilt, viewer, {"worker": str(source)}).counts == {"worker": 1}
        assert stored_read_state(comms.bus.reads) == document
        assert comms.bus.pending_count("receiver", "#team") == 1
        comms.views.mark_channel_view_read(
            "#team", worktree=str(tmp_path), through=page.newest_seq,
            expected_scope=page.display_scope,
        )
        assert counts(rebuilt, viewer, {'worker': str(source)}).counts == {"worker": 1}
        assert comms.bus.pending_count("receiver", "#team") == 1
        assert stored_read_state(comms.bus.reads).transcripts == document.transcripts
        references = tuple(message.reference for message in comms.bus.inbox("receiver"))
        comms.messaging.acknowledge("receiver", references)
        assert comms.bus.pending_count("receiver", "#team") == 0
        assert counts(rebuilt, viewer, {'worker': str(source)}).counts == {"worker": 1}
    finally:
        rebuilt.close()
