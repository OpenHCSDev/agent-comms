"""Provider-free durable pre-send selected-summary operation IDs and input gate."""

import json
import os
import subprocess
import sys

import pytest

from agent_comms.compaction_journal import (
    CompactionJournal,
    CompactionJournalError,
    CompactionJournalUnknownError,
)
from agent_comms.compaction_send_admission import native_input_admitted

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX durable journal")


@pytest.fixture
def reserved(tmp_path):
    session = tmp_path / "session.jsonl"
    session.write_text('{"type":"session","version":3}\n')
    journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
    source = {
        "source": {"witnessRevision": "dev:ino:size:mtime:ctime", "turnId": "turn"},
        "selected": {"provider": "fixture", "modelId": "model", "contextWindow": 1000},
        "settings": {"reserveTokens": 100, "keepRecentTokens": 100},
    }
    return journal, str(session), source


def test_reservation_is_durable_unresolved_and_blocks_every_input(reserved):
    journal, session, source = reserved
    operation_id = journal.reserve_selected_summary(session, source, operation_id="a" * 32)
    assert operation_id == "a" * 32
    reopened = CompactionJournal(journal.path)
    attempt = reopened.selected_summary(operation_id)
    assert attempt.status == "reserved" and attempt.commit_id is None
    assert json.loads(attempt.source_json) == source
    assert reopened.unresolved_selected_summary(session) == (attempt,)
    assert not native_input_admitted(journal.path.parent, session)
    with pytest.raises(CompactionJournalError, match="never replay"):
        reopened.reserve_selected_summary(session, source, operation_id=operation_id)
    with pytest.raises(CompactionJournalError, match="never replay"):
        reopened.reserve_selected_summary(session, source)
    reopened.mark_selected_summary_unknown(operation_id)
    assert reopened.selected_summary(operation_id).status == "unknown"
    assert not native_input_admitted(journal.path.parent, session)
    with pytest.raises(CompactionJournalError, match="Exact committed"):
        reopened.link_selected_summary_commit(operation_id, "b" * 32)


def test_link_requires_exact_committed_native_intent_binding(reserved):
    journal, session, source = reserved
    wrong = journal.begin(session, {"selectedSummaryOperationId": "0" * 32})
    journal.resolve(wrong, "committed", {"fixture": "metadata"})
    operation_id = journal.reserve_selected_summary(session, source)
    with pytest.raises(CompactionJournalError, match="Exact committed"):
        journal.link_selected_summary_commit(operation_id, wrong)
    commit_id = journal.begin(session, {"selectedSummaryOperationId": operation_id})
    with pytest.raises(CompactionJournalError, match="Exact committed"):
        journal.link_selected_summary_commit(operation_id, commit_id)
    journal.resolve(commit_id, "committed", {"fixture": "metadata"})
    journal.link_selected_summary_commit(operation_id, commit_id)
    assert journal.selected_summary(operation_id).commit_id == commit_id
    assert journal.unresolved_selected_summary(session) == ()
    assert native_input_admitted(journal.path.parent, session)
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.reserve_selected_summary(session, source, operation_id=operation_id)
    with pytest.raises(CompactionJournalError, match="transition|Exact committed"):
        journal.mark_selected_summary_unknown(operation_id)


def test_selected_attempt_does_not_block_another_session(reserved):
    journal, session, source = reserved
    other = journal.path.parent / "other-session.jsonl"
    other.write_text("{}\n")
    journal.reserve_selected_summary(session, source)
    assert native_input_admitted(journal.path.parent, str(other))
    assert not native_input_admitted(journal.path.parent, session)


def test_mark_unknown_fsync_fault_stays_unresolved(reserved, monkeypatch):
    journal, session, source = reserved
    operation_id = journal.reserve_selected_summary(session, source)
    fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(CompactionJournalUnknownError, match="never dispatch"):
        journal.mark_selected_summary_unknown(operation_id)
    monkeypatch.setattr(os, "fsync", fsync)
    assert journal.selected_summary(operation_id).status in {"reserved", "unknown"}
    assert not native_input_admitted(journal.path.parent, session)


def test_competing_native_begin_refused_unless_exact_reserved_operation_bound(reserved):
    journal, session, source = reserved
    operation_id = journal.reserve_selected_summary(session, source)
    for intent in ({}, {"selectedSummaryOperationId": "f" * 32}):
        with pytest.raises(CompactionJournalError, match="unrelated native commit"):
            journal.begin(session, intent)
    assert journal.unresolved(session) == ()
    commit_id = journal.begin(session, {"selectedSummaryOperationId": operation_id})
    assert journal.get(commit_id).status == "intent"
    assert not native_input_admitted(journal.path.parent, session)
    journal.resolve(commit_id, "aborted-no-write", {"status": "aborted-no-write"})
    journal.mark_selected_summary_unknown(operation_id)
    with pytest.raises(CompactionJournalError, match="unrelated native commit"):
        journal.begin(session, {"selectedSummaryOperationId": operation_id})


@pytest.mark.parametrize("reason", ["split_turn", "unsupported"])
def test_exact_prestart_clean_decline_permits_original_hard_backstop(reserved, reason):
    journal, session, source = reserved
    operation_id = journal.reserve_selected_summary(session, source)
    journal.decline_selected_summary_prestart(operation_id, reason)
    attempt = journal.selected_summary(operation_id)
    assert attempt.status == "declined-prestart" and attempt.decline_reason == reason
    assert native_input_admitted(journal.path.parent, session)
    with pytest.raises(CompactionJournalError, match="prestart decline"):
        journal.decline_selected_summary_prestart(operation_id, reason)


@pytest.mark.parametrize(
    "reason", ["busy", "source_mismatch", "model_mismatch", "settings_mismatch"]
)
def test_uncertain_or_changed_source_decline_is_not_a_clean_skip(reserved, reason):
    journal, session, source = reserved
    operation_id = journal.reserve_selected_summary(session, source)
    with pytest.raises(CompactionJournalError, match="not a clean skip"):
        journal.decline_selected_summary_prestart(operation_id, reason)
    journal.mark_selected_summary_unknown(operation_id)
    with pytest.raises(CompactionJournalError):
        journal.decline_selected_summary_prestart(operation_id, "split_turn")
    assert not native_input_admitted(journal.path.parent, session)


def test_native_unknown_refuses_summary_reservation(reserved):
    journal, session, source = reserved
    journal.begin(session, {"witness": "native"})
    with pytest.raises(CompactionJournalError, match="Unresolved native"):
        journal.reserve_selected_summary(session, source)
    assert journal.unresolved_selected_summary(session) == ()


def test_postcommit_fsync_fault_may_leave_blocking_summary_intent(reserved, monkeypatch):
    journal, session, source = reserved
    fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(CompactionJournalUnknownError, match="never dispatch"):
        journal.reserve_selected_summary(session, source, operation_id="b" * 32)
    monkeypatch.setattr(os, "fsync", fsync)
    assert journal.selected_summary("b" * 32).status == "reserved"
    assert not native_input_admitted(journal.path.parent, session)


def test_reservation_survives_crash_and_never_repeats_id(tmp_path):
    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    source = {
        "source": {"witnessRevision": "r"},
        "selected": {"provider": "fixture"},
        "settings": {"reserveTokens": 100},
    }
    path = tmp_path / "compaction-commits.sqlite3"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os,sys,json
from pathlib import Path
from agent_comms.compaction_journal import CompactionJournal
CompactionJournal(Path(sys.argv[1])).reserve_selected_summary(
    sys.argv[2], json.loads(sys.argv[3]), operation_id='c'*32)
os._exit(17)
""",
            str(path),
            str(session),
            json.dumps(source),
        ],
        check=False,
        timeout=10,
    )
    assert result.returncode == 17
    assert CompactionJournal(path).selected_summary("c" * 32).status == "reserved"
    assert not native_input_admitted(tmp_path, str(session))


def test_invalid_source_or_id_refuses_before_reservation(reserved):
    journal, session, source = reserved
    with pytest.raises(ValueError, match="source|Source"):
        journal.reserve_selected_summary(session, {})
    with pytest.raises(ValueError, match="operation ID"):
        journal.reserve_selected_summary(session, source, operation_id="not-hex")
    assert journal.unresolved_selected_summary(session) == ()
