"""Record-capped native reply counts, read marks, per-source publication and retirement."""
import json
import sqlite3
from concurrent.futures import Future
from multiprocessing import get_context

import pytest

from agent_comms.comms import wire
from agent_comms.execution.session_file import RegistrySessionLocation
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.roster_visibility import DEFAULT_ROSTER
from agent_comms.registry.threads import NativeThread
from agent_comms.registry.view_unread import (
    CappedCount,
    Counted,
    Indexing,
    NativeReplyPreparation,
    ReplyIndex,
    ReplyIndexUpdate,
    ReplyRead,
    ReplyReading,
    RevisionMoved,
    TranscriptReply,
)
from agent_comms.storage.private_path import FileRevision
from agent_comms.ui_model.unread import (
    CappedReplyUnread,
    EmptyCappedReplyUnread,
    ExactUnread,
    IndexingUnread,
    ReplyUnread,
    UnobservedUnread,
    UnreadPresentation,
)
from conftest import installed_comms, ui_core_read
from native_unread_fixture import completed_counts, completed_snapshot


def append(path, role='assistant', content='Reply', **fields):
    if role == 'assistant' and isinstance(content, str):
        content = [{'type': 'text', 'text': content}]
    with path.open('a') as stream:
        stream.write(json.dumps({'type': 'message', 'timestamp': '2026-10-19T05:00:00.000Z',
            'message': {'role': role, 'content': content, **fields}}) + '\n')


def session(path, identity='session-test'):
    path.write_text(json.dumps({'type': 'session', 'version': 3, 'id': identity,
        'timestamp': '2026-10-19T05:00:00.000Z', 'cwd': str(path.parent)}) + '\n')


def setup_thread(tmp_path):
    source = tmp_path / 'session.jsonl'
    session(source)
    comms = installed_comms(tmp_path / 'wire')
    comms.registry.declare(NativeThread('worker', frozenset({'ci'}),
        Checkout(tmp_path), session_file=RegistrySessionLocation.from_path(str(source), comms.root)))
    return comms, source


@pytest.mark.parametrize('left,right,expected', [
    (Counted(2), Counted(3), Counted(5)),
    (Counted(2), CappedCount(3), CappedCount(5)),
    (CappedCount(0), Counted(3), CappedCount(3)),
    (CappedCount(2), CappedCount(3), CappedCount(5)),
    (Counted(2), Indexing(), Indexing()),
    (CappedCount(2), Indexing(), Indexing()),
])
def test_count_coverage_is_preserved_when_sources_are_combined(left, right, expected):
    assert left + right == right + left == expected


@pytest.mark.parametrize('count,detail', [
    (0, ''), (1, 'at least 1 unread replies'), (32, 'at least 32 unread replies')])
def test_capped_rows_mark_unread_without_claiming_at_least_zero(count, detail):
    """A row marks that something is unread; the number stays in the tooltip."""
    shown = CappedReplyUnread.for_count(count)
    assert shown.badge == (UnreadPresentation.mark if count else '')
    assert shown.highlighted == bool(count)
    assert shown.detail == detail
    assert ReplyUnread(count).detail == f'{count} unread replies'


@pytest.mark.parametrize('unread', [
    ExactUnread(3), ReplyUnread(3), CappedReplyUnread(3), IndexingUnread()])
def test_no_row_badge_says_how_many_are_unread(unread):
    """2026-11-08: every badge printed a count, which a 40-column phone cannot spare."""
    assert unread.badge and not any(character.isdigit() for character in unread.badge)


@pytest.mark.parametrize('unread', [
    ExactUnread(0), EmptyCappedReplyUnread(), UnobservedUnread()])
def test_a_read_row_marks_nothing(unread):
    assert unread.badge == ''


def test_prepare_caps_records_not_replies_and_detects_replacement(tmp_path):
    source = tmp_path / 'session.jsonl'
    session(source)
    append(source)
    for _ in range(ReplyIndex.limit):
        append(source, 'toolResult', 'not an assistant reply', toolCallId='tool')
    revision = FileRevision.capture(source)
    index = ReplyIndex(source=str(source))
    update = index.prepare(revision, 0, get_context('spawn').Event())
    assert isinstance(update, ReplyIndexUpdate)
    assert update.after.records == ReplyIndex.limit
    assert update.after.start > 0
    assert update.replies == ()
    assert update.after.counted(0, 0) == CappedCount(0)
    rendered = {}
    update.after.counted(0, 0).record('worker', rendered)
    assert rendered == {'worker': EmptyCappedReplyUnread()}
    replacement = tmp_path / 'replacement'
    session(replacement, 'replacement')
    replacement.replace(source)
    assert isinstance(index.prepare(revision, 0, get_context('spawn').Event()), RevisionMoved)


def test_reply_counts_are_persistent_human_read_positions(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source, 'user', 'My own prompt')
    append(source, content=[{'type': 'thinking', 'thinking': 'private'}])
    append(source, content=[{'type': 'toolCall', 'id': 'tool', 'name': 'read', 'arguments': {}}])
    append(source, 'toolResult', 'tool output', toolCallId='tool')
    append(source, content=[{'type': 'text', 'text': 'one'}, {'type': 'text', 'text': 'two'}])
    assert completed_snapshot(comms, Checkout(tmp_path)).thread_unread['worker'] == ReplyUnread(1)
    shown = comms.transcripts.transcript_checkpoint('worker')
    append(source, content='Arrived after the displayed cut')
    comms.views.mark_thread_view_read('worker', worktree=Checkout(tmp_path), through=shown)
    assert completed_snapshot(wire(comms.root), Checkout(tmp_path)).thread_unread['worker'] == ReplyUnread(1)
    assert completed_snapshot(comms, Checkout(tmp_path / 'other-project')).thread_unread['worker'] == ReplyUnread(1)
    reply = comms.messaging.send('worker', '#ci', 'Channel reply').message
    comms.views.mark_user_view_read('#ci', worktree=Checkout(tmp_path))
    comms.bus.mark_read_through('worker', reply.seq)
    assert completed_snapshot(comms, Checkout(tmp_path)).thread_unread['worker'] == ReplyUnread(1)


def test_read_uses_the_captured_registry_after_a_thread_is_archived(tmp_path):
    comms, source = setup_thread(tmp_path)
    captured = comms.registry.snapshot()
    comms.registry.archive('worker')
    state = comms.views.transcript_reads
    shown = state.counts('viewer', registry=captured, document=state.reads.read(), visibility=DEFAULT_ROSTER)
    assert set(shown.counts) == {'worker'}
    latest = state.counts('viewer', registry=comms.registry.snapshot(), document=state.reads.read(), visibility=DEFAULT_ROSTER)
    assert set(latest.counts) == set()
    requested = comms.views.thread_presentations(('worker',))
    assert requested.views['worker'].presentation.summary == 'Archived'


def test_read_uses_the_captured_read_document(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    state = comms.views.transcript_reads
    viewer = comms.messaging.user_identity(Checkout(tmp_path)).name
    assert completed_counts(state, viewer).counts == {'worker': ReplyUnread(1)}
    document = state.reads.read()
    shown = comms.transcripts.transcript_checkpoint('worker')
    comms.views.mark_thread_view_read('worker', worktree=Checkout(tmp_path), through=shown)
    assert state.counts(viewer, registry=comms.registry.snapshot(), document=document, visibility=DEFAULT_ROSTER).counts == {'worker': ReplyUnread(1)}
    assert completed_counts(state, viewer).counts == {'worker': ReplyUnread(0)}


def test_completed_source_publishes_while_another_source_is_still_pending(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    second = tmp_path / 'second.jsonl'
    session(second, 'second')
    append(second)
    state = comms.views.transcript_reads
    database = state._database()
    reads = {str(path): ReplyRead(ReplyIndex(source=str(path)), FileRevision.capture(path), 0)
             for path in (source, second)}
    futures = {name: Future() for name in reads}
    preparation = NativeReplyPreparation({name: ReplyReading(read, futures[name]) for name, read in reads.items()})
    state._preparation = preparation
    observed = []
    subscribed = state.watch(frozenset(), observed.append)
    assert subscribed == frozenset(futures.values())
    first = reads[str(source)]
    futures[str(source)].set_result(first.index.prepare(first.revision, 0, get_context('spawn').Event()))
    state._preparation = preparation.finished(state, database, reads)
    assert not futures[str(second)].done()
    assert observed == [futures[str(source)]]
    assert TranscriptReply.after(database, {str(source): 0, str(second): 0}) == {str(source): 1, str(second): 0}
    last = reads[str(second)]
    futures[str(second)].set_result(last.index.prepare(last.revision, 0, get_context('spawn').Event()))
    state._preparation = preparation.finished(state, database, reads)
    assert state.preparation is state._idle
    assert observed == list(futures.values())
    assert TranscriptReply.after(database, {str(source): 0, str(second): 0}) == {str(source): 1, str(second): 1}


def test_busy_index_keeps_the_completed_source_for_a_later_publication(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    state = comms.views.transcript_reads
    state.counts('viewer', registry=comms.registry.snapshot(), document=state.reads.read(), visibility=DEFAULT_ROSTER)
    for future in state.watch(frozenset(), lambda _: None):
        future.result(timeout=10)
    preparation = state.preparation
    with sqlite3.connect(state.index_path) as writer:
        writer.execute('BEGIN IMMEDIATE')
        assert preparation.finished(state, state._database(), (str(source),)) is preparation
        assert preparation.readers
    assert completed_counts(state, 'viewer').counts == {'worker': ReplyUnread(1)}


def test_source_replacement_and_partial_tail_do_not_lose_replies(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    state = comms.views.transcript_reads
    assert completed_counts(state, 'viewer').counts == {'worker': ReplyUnread(1)}
    with source.open('ab') as stream:
        stream.write(b'{"type":"message","message":{"role":"assistant","content":')
    assert completed_counts(state, 'viewer').counts == {'worker': ReplyUnread(1)}
    with source.open('ab') as stream:
        stream.write(b'[{"type":"text","text":"finished"}]},"timestamp":"2026-10-19T05:00:00.000Z"}\n')
    assert completed_counts(state, 'viewer').counts == {'worker': ReplyUnread(2)}
    replacement = tmp_path / 'replacement'
    session(replacement, 'replacement')
    append(replacement)
    replacement.replace(source)
    assert completed_counts(state, 'viewer').counts == {'worker': ReplyUnread(1)}


def test_oversized_record_is_not_truncated_or_limited_as_input(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source, content='x' * 300_000)
    assert completed_counts(comms.views.transcript_reads, 'viewer').counts == {'worker': ReplyUnread(1)}


def test_a_closed_reader_is_not_reused_and_other_views_keep_their_marks(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    state = comms.views.transcript_reads
    assert completed_counts(state, 'one').counts == {'worker': ReplyUnread(1)}
    state.close()
    fresh = wire(comms.root).views.transcript_reads
    assert fresh is not state
    assert completed_counts(fresh, 'two').counts == {'worker': ReplyUnread(1)}
    with pytest.raises(RuntimeError):
        state.counts('one', registry=comms.registry.snapshot(), document=state.reads.read(), visibility=DEFAULT_ROSTER)


def test_a_ui_marks_a_thread_read_through_core(tmp_path):
    """The owner's acknowledgement crosses the wire as a request and lands as the local one."""
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.transport.tools.sidebar import CommsMarkThreadViewReadTool

    comms, source = setup_thread(tmp_path)
    append(source)
    state = comms.views.transcript_reads
    viewer = comms.messaging.user_identity(Checkout(tmp_path)).name
    assert completed_counts(state, viewer).counts == {'worker': ReplyUnread(1)}
    request = CommsMarkThreadViewReadTool(
        thread='worker', through=comms.transcripts.transcript_checkpoint('worker'))
    ui_core_read(comms, 
        FieldCodec.decode(CommsMarkThreadViewReadTool, FieldCodec.encode(request)))
    assert completed_counts(state, viewer).counts == {'worker': ReplyUnread(0)}
