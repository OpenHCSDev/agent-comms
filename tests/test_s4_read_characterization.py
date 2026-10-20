"""Current S4 Part B: authority lifetime and disposable-index recovery."""

from agent_comms.channel_targets import CommandTarget, NativeThreadTarget
from agent_comms.coordination_errors import DMScopeChanged
from agent_comms.read_basis import NamedViewer
from agent_comms.read_ledger import ReadLedger
from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.threads import Thread
from agent_comms.ui_model.unread import ReplyUnread
from native_unread_fixture import native_reader
from agent_comms.registry_document import DEFAULT_ROSTER
from native_unread_fixture import completed_counts, completed_snapshot
from test_thread_unread import append


def test_replaced_human_incarnation_rejects_painted_proof_and_prior_reads(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('worker', frozenset({'team'}), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    message = comms.messaging.send("worker", "#team", "painted").message
    page = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name))
    comms.views.mark_page_read(page, worktree=str(tmp_path))
    assert message.seq in comms.bus.reads.cursor(viewer, comms.registry.snapshot()).for_conversation(ReadLedger.conversation(message, comms.registry.snapshot()))
    original = comms.registry.require(viewer)
    comms.registry.unregister(viewer)
    comms.registry.remove(viewer)
    comms.registry.register(replace(original, created_at=original.created_at + 1))
    assert message.seq not in comms.bus.reads.cursor(viewer, comms.registry.snapshot()).for_conversation(ReadLedger.conversation(message, comms.registry.snapshot()))
    with pytest.raises(DMScopeChanged):
        comms.views.mark_page_read(page, worktree=str(tmp_path))
    refreshed = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name))
    comms.views.mark_page_read(refreshed, worktree=str(tmp_path))
    assert message.seq in comms.bus.reads.cursor(viewer, comms.registry.snapshot()).for_conversation(ReadLedger.conversation(message, comms.registry.snapshot()))


def test_index_rebuild_preserves_distinct_transcript_bus_and_delivery_facts(tmp_path):
    comms = wire(tmp_path / "wire")
    source = tmp_path / "native.jsonl"
    append(source, content="painted reply")
    comms.registry.declare(comms.registry.new_thread('worker', frozenset({'team'}), str(tmp_path), session_file=str(source)))
    comms.registry.declare(comms.registry.new_thread('receiver', frozenset({'team'}), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    shown = comms.transcripts.transcript_checkpoint("worker")
    append(source, content="unpainted reply")
    message = comms.messaging.send("worker", "#team", "unpainted bus row").message
    page = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name))
    assert message.seq not in comms.bus.reads.cursor(viewer, comms.registry.snapshot()).for_conversation(ReadLedger.conversation(message, comms.registry.snapshot()))
    comms.views.mark_thread_view_read("worker", worktree=str(tmp_path), through=shown)
    assert completed_snapshot(comms, str(tmp_path)).unread[NativeThreadTarget('worker')].count == 1
    document = comms.bus.reads.read()
    assert document.transcripts and not document.messages
    index_owner = comms.views.transcript_reads
    index_owner.close()
    index_owner.index_path.unlink()
    rebuilt = native_reader(comms.bus.reads.path)
    expected_counts = {"worker": ReplyUnread(1), "receiver": ReplyUnread(0)}
    try:
        assert completed_counts(rebuilt, viewer).counts == expected_counts
        assert comms.bus.reads.read() == document
        assert comms.bus.pending_count("receiver", CommandTarget.named("#team")) == 1
        comms.views.mark_page_read(page, worktree=str(tmp_path))
        assert rebuilt.counts(viewer, registry=comms.registry.snapshot(), document=rebuilt.reads.read(), visibility=DEFAULT_ROSTER).counts == expected_counts
        assert comms.bus.pending_count("receiver", CommandTarget.named("#team")) == 1
        assert comms.bus.reads.read().transcripts == document.transcripts
        references = tuple(message.reference for message in comms.bus.inbox("receiver"))
        comms.messaging.acknowledge("receiver", references)
        assert comms.bus.pending_count("receiver", CommandTarget.named("#team")) == 0
        assert rebuilt.counts(viewer, registry=comms.registry.snapshot(), document=rebuilt.reads.read(), visibility=DEFAULT_ROSTER).counts == expected_counts
    finally:
        rebuilt.close()
