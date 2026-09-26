"""Durable intent state machine, provider-free and independent of native Pi."""

import json
import os
import sqlite3
import stat
import subprocess
import sys

import pytest

from agent_comms.compaction_journal import (
    CompactionJournal,
    CompactionJournalError,
    CompactionJournalUnknownError,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX production bridge")


@pytest.fixture
def journal(tmp_path):
    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    return CompactionJournal(tmp_path / "journal.sqlite3"), str(session)


def test_effective_extra_and_delete_are_verified(journal):
    journal, _ = journal
    with journal._transaction() as db:
        assert db.execute("PRAGMA synchronous").fetchone() == (3,)
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)


def test_every_commit_syncs_directory_after_constructor(journal, monkeypatch):
    journal, session = journal
    fsync = os.fsync
    directories = []

    def observed(fd):
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
        directories.append(fd)
        fsync(fd)

    monkeypatch.setattr(os, "fsync", observed)
    commit_id = journal.begin(session, {})
    assert len(directories) == 1
    journal.resolve(commit_id, "unknown", {})
    assert len(directories) == 2
    journal.get(commit_id)
    assert len(directories) == 3


def test_postcommit_sync_fault_is_unknown_not_accepted_intent(journal, monkeypatch):
    journal, session = journal
    fsync = os.fsync

    def denied(fd):
        raise OSError("post-commit directory sync fault")

    monkeypatch.setattr(os, "fsync", denied)
    with pytest.raises(CompactionJournalUnknownError, match="never dispatch"):
        journal.begin(session, {}, commit_id="a" * 32)
    monkeypatch.setattr(os, "fsync", fsync)
    assert journal.get("a" * 32).status == "intent"
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.begin(session, {})


def test_ineffective_durability_mode_refused_before_transaction(journal, monkeypatch):
    journal, session = journal
    connect = sqlite3.connect

    class WrongMode(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement == "PRAGMA synchronous":
                return super().execute("SELECT 2")
            return super().execute(statement, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, **kw, factory=WrongMode))
    with pytest.raises(CompactionJournalError, match="mode unavailable"):
        journal.begin(session, {})
    monkeypatch.setattr(sqlite3, "connect", connect)
    assert journal.unresolved(session) == ()


def test_intent_survives_process_death_and_blocks_new_dispatch(tmp_path):
    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    db = tmp_path / "journal.sqlite3"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os,sys
from pathlib import Path
from agent_comms.compaction_journal import CompactionJournal
journal = CompactionJournal(Path(sys.argv[1]))
journal.begin(sys.argv[2], {'payloadDigest':'digest'}, commit_id='a'*32)
os._exit(17)
""",
            str(db),
            str(session),
        ],
        check=False,
    )
    assert result.returncode == 17
    journal = CompactionJournal(db)
    operation = journal.get("a" * 32)
    assert operation.status == "intent"
    assert json.loads(operation.intent_json) == {"payloadDigest": "digest"}
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.begin(str(session), {})


@pytest.mark.parametrize("outcome", ["committed", "refused", "aborted-no-write"])
def test_terminal_is_immutable_and_id_never_reusable(journal, outcome):
    journal, session = journal
    commit_id = journal.begin(session, {})
    journal.resolve(commit_id, outcome, {"proof": "fixture"})
    with pytest.raises(CompactionJournalError):
        journal.resolve(commit_id, "unknown", {})
    with pytest.raises(CompactionJournalError, match="reused commit ID"):
        journal.begin(session, {}, commit_id=commit_id)
    assert journal.begin(session, {}) != commit_id


def test_unknown_requires_reconciliation_not_refusal(journal):
    journal, session = journal
    commit_id = journal.begin(session, {})
    journal.resolve(commit_id, "unknown", {})
    with pytest.raises(CompactionJournalError):
        journal.resolve(commit_id, "refused", {})
    with pytest.raises(CompactionJournalError):
        journal.begin(session, {})
    journal.resolve(commit_id, "committed", {"entryId": "native"})
    assert journal.get(commit_id).status == "committed"


def test_metadata_only_outbox_is_commit_id_keyed_and_atomic(journal, tmp_path):
    journal, session = journal
    commit_id = journal.begin(session, {"summary": "secret never published"})
    assert journal.pending_publications(session) == ()
    journal.resolve(commit_id, "unknown", {"status": "unknown", "reason": "deadline"})
    assert journal.pending_publications(session) == ()
    metadata = {"status": "committed", "entryId": "entry-1", "revision": "r1", "leafId": "leaf"}
    journal.resolve(commit_id, "committed", metadata, publication=True)
    reopened = CompactionJournal(journal.path)
    pending = reopened.pending_publications(session)
    assert len(pending) == 1 and pending[0].commit_id == commit_id
    assert json.loads(pending[0].metadata_json) == {
        "commitId": commit_id,
        "entryId": "entry-1",
        "revision": "r1",
        "leafId": "leaf",
    }
    assert "secret" not in pending[0].metadata_json
    assert not hasattr(pending[0], "recipient")
    with pytest.raises(CompactionJournalError, match="changed publication"):
        reopened.observe_publication(commit_id, "{}")
    assert len(reopened.pending_publications(session)) == 1
    reopened.observe_publication(commit_id, pending[0].metadata_json)
    reopened.observe_publication(commit_id, pending[0].metadata_json)
    assert CompactionJournal(journal.path).pending_publications(session) == ()
    second = journal.begin(session, {})
    journal.resolve(
        second,
        "committed",
        {"status": "committed", "entryId": "entry-2", "revision": "r2", "leafId": "leaf-2"},
        publication=True,
    )
    assert [event.commit_id for event in journal.pending_publications(session)] == [second]


def test_observe_postcommit_parent_fsync_unknown_can_already_be_observed(journal, monkeypatch):
    journal, session = journal
    commit_id = journal.begin(session, {})
    journal.resolve(
        commit_id,
        "committed",
        {"status": "committed", "entryId": "native", "revision": "r", "leafId": "leaf"},
        publication=True,
    )
    pending = journal.pending_publications(session)
    assert len(pending) == 1
    original_fsync = os.fsync

    def deny_directory(fd):
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
        raise OSError("post-commit directory sync fault")

    monkeypatch.setattr(os, "fsync", deny_directory)
    with pytest.raises(CompactionJournalUnknownError, match="UNKNOWN"):
        journal.observe_publication(commit_id, pending[0].metadata_json)
    monkeypatch.setattr(os, "fsync", original_fsync)
    # No rollback is promised once SQLite COMMIT returns. Reconcile exact ID
    # without sending the native operation or projection a second time.
    reopened = CompactionJournal(journal.path)
    assert reopened.pending_publications(session) == ()
    with sqlite3.connect(journal.path) as db:
        assert db.execute(
            "SELECT status FROM publications WHERE commit_id = ?", (commit_id,)
        ).fetchone() == ("observed",)
    assert reopened.get(commit_id).status == "committed"


def test_changed_publication_metadata_refuses_local_projection(journal):
    journal, session = journal
    commit_id = journal.begin(session, {})
    journal.resolve(
        commit_id,
        "committed",
        {"status": "committed", "entryId": "native", "revision": "r", "leafId": "leaf"},
        publication=True,
    )
    with sqlite3.connect(journal.path) as db:
        db.execute(
            "UPDATE publications SET metadata_json = ? WHERE commit_id = ?",
            (json.dumps({"commitId": commit_id, "summary": "forged leak"}), commit_id),
        )
    with pytest.raises(CompactionJournalError, match="metadata changed"):
        journal.pending_publications(session)


def test_no_publication_without_exact_committed_native_evidence(journal):
    journal, session = journal
    commit_id = journal.begin(session, {})
    with pytest.raises(CompactionJournalError, match="Exact committed"):
        journal.resolve(
            commit_id, "committed", {"status": "committed", "entryId": "x"}, publication=True
        )
    assert journal.get(commit_id).status == "intent"
    journal.resolve(commit_id, "aborted-no-write", {"status": "aborted-no-write"})
    assert journal.pending_publications(session) == ()


def test_session_alias_cannot_bypass_unresolved_intent(journal, tmp_path):
    journal, session = journal
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to(session)
    journal.begin(session, {})
    with pytest.raises(CompactionJournalError):
        journal.begin(str(alias), {})


def test_durable_intent_does_not_retain_mutable_caller_data(journal):
    journal, session = journal
    intent = {"witness": {"leaf": "original"}}
    commit_id = journal.begin(session, intent)
    intent["witness"]["leaf"] = "forged"
    assert json.loads(journal.get(commit_id).intent_json)["witness"]["leaf"] == "original"
