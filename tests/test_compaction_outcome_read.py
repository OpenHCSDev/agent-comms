"""Original journal projection and SQLite read custody, provider-free."""

import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.compaction_errors import CompactionJournalError
from agent_comms.compaction_identity import SelectedCommitReference
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.transcript_events import CompactionOutcomeTranscript
from agent_comms.compaction_records import CompactionOperation
from agent_comms.compaction_states import CommittedNativeOutcome, CommittedOperation, RetiredUnknownSummary
from agent_comms.coordination_errors import StaleRevision
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions
from agent_comms.registry_document import RegistryDocument
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.threads import Thread
from selected_summary_cases import manual_source


@pytest.fixture
def original(tmp_path):
    session = tmp_path / 'session.jsonl'
    session.write_text('{"type":"session","version":3}\n')
    journal = CompactionJournal(tmp_path / 'compaction-commits.sqlite3')
    incarnation = ThreadIncarnation('owner', 1.)
    registry = RegistryDocument(threads={
        'owner': Thread('owner', frozenset(), str(tmp_path), created_at=1.),
    }).snapshot()
    source = {'source': manual_source(session, incarnation=incarnation),
              'selected': {'provider': 'fixture', 'modelId': 'model', 'contextWindow': 1000},
              'settings': {'reserveTokens': 100, 'keepRecentTokens': 100}}
    return journal, session, incarnation, registry, source


def observe(original):
    journal, session, incarnation, registry, _ = original
    return CompactionJournal.snapshot(journal.path, str(session), incarnation, registry)


def test_original_state_changes_below_frontier_are_scoped_and_read_only(original, monkeypatch):
    journal, session, incarnation, registry, source = original
    first = journal.summaries.reserve(str(session), source)
    empty = observe(original)
    assert not empty.outcomes
    journal.summaries.mark_unknown(first)
    first_view = observe(original)
    row, = first_view.outcomes
    assert row.identity == journal.summaries.get(first).identity
    assert row.native_offset == session.stat().st_size
    assert isinstance(row.event(), CompactionOutcomeTranscript)
    assert row.event().timestamp is None
    assert 'UNKNOWN' in row.text
    assert FieldCodec.decode(type(row.event()), FieldCodec.encode(row.event())) == row.event()
    assert first_view.revision != empty.revision
    row.require_native_source(session, session.stat().st_size)
    with pytest.raises(StaleRevision, match='cut'):
        row.require_native_source(session, row.native_offset - 1)

    other = session.with_name('other.jsonl')
    other.write_bytes(session.read_bytes())
    unrelated_source = dict(source, source=manual_source(other))
    later = journal.summaries.reserve(str(other), unrelated_source)
    journal.summaries.mark_unknown(later)
    assert observe(original).revision == first_view.revision
    # A changed older outcome matters even when a later unrelated row exists.
    journal.summaries.retire_unchanged(journal.summaries.get(first))
    retired = observe(original)
    assert retired.outcomes[0].attempt.state == RetiredUnknownSummary()
    assert retired.revision == first_view.revision  # Same truthful projected UNKNOWN.

    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in journal.path.parent.iterdir() if p.is_file()}
    monkeypatch.setattr(CompactionJournal, '__init__', lambda *a: pytest.fail('writer constructed'))
    monkeypatch.setattr(os, 'fsync', lambda *a: pytest.fail('read attempted fsync'))
    assert observe(original) == retired
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
             for p in journal.path.parent.iterdir() if p.is_file()}
    assert after == before


def test_frozen_original_incarnation_survives_rename_but_not_name_reuse(original):
    journal, session, incarnation, registry, source = original
    operation = journal.summaries.reserve(str(session), source)
    journal.summaries.fail(operation, 'provider-control-failed')
    renamed = replace(registry, threads={'renamed': replace(registry.threads['owner'], name='renamed')},
                      aliases={'owner': 'renamed'})
    assert CompactionJournal.snapshot(journal.path, str(session),
                                     ThreadIncarnation('renamed', 1.), renamed).outcomes
    reused = replace(registry, threads={'owner': replace(registry.threads['owner'], created_at=2.)})
    assert not CompactionJournal.snapshot(journal.path, str(session),
                                         ThreadIncarnation('owner', 2.), reused).outcomes


def test_only_exact_committed_native_link_supersedes_uncertainty(original):
    journal, session, incarnation, registry, source = original
    operation = journal.summaries.reserve(str(session), source)
    attempt = journal.summaries.get(operation)
    reference = SelectedCommitReference(operation, TextDigest.of(attempt.source_json).value)
    with InputDispositions(session.parent / InputDispositions.filename).reading() as inputs:
        commit_id = journal.operations.begin(str(session), FieldCodec.encode(reference), inputs=inputs)
    journal.summaries.mark_unknown(operation)
    assert observe(original).outcomes
    revision = attempt.request.source.reserved_revision.native
    journal.operations.resolve(commit_id, CommittedOperation(), FieldCodec.encode(
        CommittedNativeOutcome('native-entry', revision, 'native-entry', 'a' * 64)))
    assert not observe(original).outcomes
    # Corrupt source linkage must not manufacture committed coverage.
    wrong = replace(reference, source_digest='b' * 64)
    with journal.transaction() as db:
        CompactionOperation.update(db, where='commit_id=?', parameters=(commit_id,),
                                   intent_json=json.dumps(FieldCodec.encode(wrong)))
    with pytest.raises(CompactionJournalError, match='source digest'):
        observe(original)


def test_absent_and_unsafe_stores_do_not_create_or_mutate_sqlite(tmp_path):
    absent = tmp_path / 'missing' / 'compaction-commits.sqlite3'
    snapshot = RegistryDocument().snapshot()
    assert not CompactionJournal.snapshot(absent, '/original', ThreadIncarnation('owner', 1.), snapshot).outcomes
    assert not absent.parent.exists()
    dangling = tmp_path / 'dangling'
    dangling.symlink_to(absent)
    with pytest.raises(CompactionJournalError, match='regular'):
        CompactionJournal.snapshot(dangling, '/original', ThreadIncarnation('owner', 1.), snapshot)
    wal = tmp_path / 'wal.sqlite3'
    with sqlite3.connect(wal) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE control(value INTEGER)')
    before = set(tmp_path.iterdir())
    with pytest.raises(CompactionJournalError, match='rollback format'):
        CompactionJournal.snapshot(wal, '/original', ThreadIncarnation('owner', 1.), snapshot)
    assert set(tmp_path.iterdir()) == before


def test_native_append_preserves_cut_but_replacement_and_truncation_do_not(original):
    journal, session, incarnation, registry, source = original
    operation = journal.summaries.reserve(str(session), source)
    journal.summaries.mark_unknown(operation)
    row, = observe(original).outcomes
    with session.open('a') as stream:
        stream.write('genuine later bytes\n')
    row.require_native_source(session, session.stat().st_size)
    session.write_text('')
    with pytest.raises(StaleRevision, match='cut'):
        row.require_native_source(session, 0)
    replacement = session.with_name('replacement')
    replacement.write_text('replacement native source larger than original\n')
    replacement.replace(session)
    with pytest.raises(StaleRevision, match='inode'):
        row.require_native_source(session, session.stat().st_size)
