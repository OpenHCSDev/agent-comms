"""Record-capped native reply counts and independent source preparation.

The old prefix-resume/time-budget tests described deleted owners. These checks
exercise the current suffix, read marks, per-source publication and retirement.
"""
import json
import sqlite3
from concurrent.futures import Future
from multiprocessing import get_context
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_comms.comms import wire
from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.private_path import FileRevision
from agent_comms.registry_document import DEFAULT_ROSTER
from agent_comms.ui_model.unread import CappedReplyUnread, EmptyCappedReplyUnread, ReplyUnread
from agent_comms.view_unread import (
    CappedCount, Counted, Indexing, NativeReplyPreparation, ReplyIndex,
    ReplyIndexUpdate, ReplyRead, ReplyReading, RevisionMoved, TranscriptReply,
)
from native_unread_fixture import completed_counts, completed_snapshot, native_reader


def append(path, role='assistant', content='Reply'):
    if role == 'assistant' and isinstance(content, str):
        content = [{'type': 'text', 'text': content}]
    with path.open('a') as stream:
        stream.write(json.dumps({'type': 'message', 'timestamp': '2026-10-19T05:00:00.000Z',
            'message': {'role': role, 'content': content}}) + '\n')


def session(path, identity='session-test'):
    path.write_text(json.dumps({'type': 'session', 'version': 3, 'id': identity,
        'timestamp': '2026-10-19T05:00:00.000Z', 'cwd': str(path.parent)}) + '\n')


def setup_thread(tmp_path):
    source = tmp_path / 'session.jsonl'
    session(source)
    comms = wire(tmp_path / 'wire')
    comms.registry.declare(comms.registry.new_thread('worker', frozenset({'ci'}),
        str(tmp_path), session_file=str(source)))
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


@pytest.mark.parametrize('count,label', [(0, ''), (1, '(1+)'), (32, '(32+)')])
def test_capped_labels_never_claim_at_least_zero(count, label):
    shown = CappedReplyUnread.for_count(count)
    assert shown.label == label
    assert shown.highlighted == bool(count)
    assert ReplyUnread(count).label == f'({count})'


def test_prepare_caps_records_not_replies_and_detects_replacement(tmp_path):
    source = tmp_path / 'session.jsonl'
    session(source)
    append(source)
    for _ in range(ReplyIndex.limit):
        append(source, 'toolResult', 'not an assistant reply')
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
    append(source, 'toolResult', 'tool output')
    append(source, content=[{'type': 'text', 'text': 'one'}, {'type': 'text', 'text': 'two'}])
    assert completed_snapshot(comms, str(tmp_path)).thread_unread['worker'] == ReplyUnread(1)
    shown = comms.transcripts.transcript_checkpoint('worker')
    append(source, content='Arrived after the displayed cut')
    comms.views.mark_thread_view_read('worker', worktree=str(tmp_path), through=shown)
    assert completed_snapshot(wire(comms.root), str(tmp_path)).thread_unread['worker'] == ReplyUnread(1)
    assert completed_snapshot(comms, str(tmp_path / 'other-project')).thread_unread['worker'] == ReplyUnread(1)
    comms.messaging.send('worker', '#ci', 'Channel reply')
    comms.views.mark_user_view_read('#ci', worktree=str(tmp_path))
    comms.messaging.acknowledge('worker', tuple(m.reference for m in comms.bus.inbox('worker')))
    assert completed_snapshot(comms, str(tmp_path)).thread_unread['worker'] == ReplyUnread(1)


def test_idle_reads_use_the_index_and_changed_source_recounts_only_the_cap(tmp_path):
    comms, source = setup_thread(tmp_path)
    for _ in range(100):
        append(source)
    expected = {'worker': CappedReplyUnread(ReplyIndex.limit)}
    state = comms.views.transcript_reads
    assert completed_counts(state, 'viewer').counts == expected
    with patch.object(ReplyIndex, 'prepare', side_effect=AssertionError('idle reparse')):
        assert state.counts('viewer', document=state.reads.read(), visibility=DEFAULT_ROSTER).counts == expected
        assert state.preparation is state._idle
    append(source)
    assert completed_counts(state, 'viewer').counts == expected
    fresh = native_reader(state.path)
    try:
        with patch.object(ReplyIndex, 'prepare', side_effect=AssertionError('reopen reparse')):
            assert fresh.counts('viewer', document=fresh.reads.read(), visibility=DEFAULT_ROSTER).counts == expected
            assert fresh.preparation is fresh._idle
    finally:
        fresh.close()


def test_read_uses_the_captured_read_document(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    state = comms.views.transcript_reads
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    assert completed_counts(state, viewer).counts == {'worker': ReplyUnread(1)}
    document = state.reads.read()
    shown = comms.transcripts.transcript_checkpoint('worker')
    comms.views.mark_thread_view_read('worker', worktree=str(tmp_path), through=shown)
    assert state.counts(viewer, document=document, visibility=DEFAULT_ROSTER).counts == {'worker': ReplyUnread(1)}
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
    state.counts('viewer', document=state.reads.read(), visibility=DEFAULT_ROSTER)
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
    with pytest.raises(RuntimeError, match='closed'):
        state.counts('one', document=state.reads.read(), visibility=DEFAULT_ROSTER)
