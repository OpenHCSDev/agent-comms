"""Durable intent state machine, provider-free and independent of native Pi."""

import json
import os
import sqlite3
import stat
import subprocess
import sys

import pytest

from agent_comms.compaction_errors import CompactionJournalError, CompactionJournalUnknownError
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_states import (
    AbortedNoWriteOperation,
    CommittedOperation,
    OperationState,
    RefusedOperation,
    UnknownOperation,
)
from agent_comms.input_disposition import InputDispositions

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX production bridge")


@pytest.fixture
def journal(tmp_path):
    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    return CompactionJournal(tmp_path / "journal.sqlite3"), str(session)


def test_effective_extra_and_delete_are_verified(journal):
    journal, _ = journal
    with journal.transaction() as db:
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
    commit_id = journal.operations.begin(
        session,
        {},
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    assert len(directories) == 1
    journal.operations.resolve(commit_id, UnknownOperation(), {})
    assert len(directories) == 2
    journal.operations.get(commit_id)
    assert len(directories) == 3


def test_postcommit_sync_fault_is_unknown_not_accepted_intent(journal, monkeypatch):
    journal, session = journal
    fsync = os.fsync

    def denied(fd):
        raise OSError("post-commit directory sync fault")

    monkeypatch.setattr(os, "fsync", denied)
    with pytest.raises(CompactionJournalUnknownError, match="never dispatch"):
        journal.operations.begin(
            session,
            {},
            commit_id="a" * 32,
            inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
        )
    monkeypatch.setattr(os, "fsync", fsync)
    assert journal.operations.get("a" * 32).state.declared_name == "intent"
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.operations.begin(
            session,
            {},
            inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
        )


def test_ineffective_durability_mode_refused_before_transaction(journal, monkeypatch):
    journal, session = journal
    connect = sqlite3.connect

    class WrongMode(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement == "PRAGMA synchronous":
                return super().execute("SELECT 2 AS synchronous")
            return super().execute(statement, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, **kw, factory=WrongMode))
    with pytest.raises(CompactionJournalError, match="mode unavailable"):
        journal.operations.begin(
            session,
            {},
            inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
        )
    monkeypatch.setattr(sqlite3, "connect", connect)
    assert journal.operations.unresolved(session) == ()


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
from agent_comms.input_disposition import InputDispositions
journal = CompactionJournal(Path(sys.argv[1]))
journal.operations.begin(sys.argv[2], {'payloadDigest':'digest'}, commit_id='a'*32, inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read())
os._exit(17)
""",
            str(db),
            str(session),
        ],
        check=False,
    )
    assert result.returncode == 17
    journal = CompactionJournal(db)
    operation = journal.operations.get("a" * 32)
    assert operation.state.declared_name == "intent"
    assert json.loads(operation.intent_json) == {"payloadDigest": "digest"}
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.operations.begin(
            str(session),
            {},
            inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
        )


@pytest.mark.parametrize("outcome", ["committed", "refused", "aborted-no-write"])
def test_terminal_is_immutable_and_id_never_reusable(journal, outcome):
    journal, session = journal
    commit_id = journal.operations.begin(
        session,
        {},
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    journal.operations.resolve(commit_id, OperationState.decode(outcome)(), {"proof": "fixture"})
    with pytest.raises(CompactionJournalError):
        journal.operations.resolve(commit_id, UnknownOperation(), {})
    with pytest.raises(CompactionJournalError, match="reused commit ID"):
        journal.operations.begin(
            session,
            {},
            commit_id=commit_id,
            inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
        )
    assert (
        journal.operations.begin(
            session,
            {},
            inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
        )
        != commit_id
    )


def test_unknown_requires_reconciliation_not_refusal(journal):
    journal, session = journal
    commit_id = journal.operations.begin(
        session,
        {},
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    journal.operations.resolve(commit_id, UnknownOperation(), {})
    with pytest.raises(CompactionJournalError):
        journal.operations.resolve(commit_id, RefusedOperation(), {})
    with pytest.raises(CompactionJournalError):
        journal.operations.begin(
            session,
            {},
            inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
        )
    journal.operations.resolve(commit_id, CommittedOperation(), {"entryId": "native"})
    assert journal.operations.get(commit_id).state.declared_name == "committed"


def test_metadata_only_outbox_is_commit_id_keyed_and_atomic(journal, tmp_path):
    journal, session = journal
    commit_id = journal.operations.begin(
        session,
        {"summary": "secret never published"},
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    assert journal.publications.pending(session) == ()
    journal.operations.resolve(commit_id, UnknownOperation(), {"status": "unknown", "reason": "deadline"})
    assert journal.publications.pending(session) == ()
    metadata = {
        "status": "committed",
        "metadataDigest": "0" * 64,
        "entryId": "entry-1",
        "revision": "r1",
        "leafId": "leaf",
    }
    journal.operations.resolve(commit_id, CommittedOperation(), metadata, publication=True)
    reopened = CompactionJournal(journal.path)
    pending = reopened.publications.pending(session)
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
        reopened.publications.observe(commit_id, "{}")
    assert len(reopened.publications.pending(session)) == 1
    reopened.publications.observe(commit_id, pending[0].metadata_json)
    reopened.publications.observe(commit_id, pending[0].metadata_json)
    assert CompactionJournal(journal.path).publications.pending(session) == ()
    second = journal.operations.begin(
        session,
        {},
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    journal.operations.resolve(
        second,
        CommittedOperation(),
        {
            "status": "committed",
            "metadataDigest": "0" * 64,
            "entryId": "entry-2",
            "revision": "r2",
            "leafId": "leaf-2",
        },
        publication=True,
    )
    assert [event.commit_id for event in journal.publications.pending(session)] == [second]


def test_observe_postcommit_parent_fsync_unknown_can_already_be_observed(journal, monkeypatch):
    journal, session = journal
    commit_id = journal.operations.begin(
        session,
        {},
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    journal.operations.resolve(
        commit_id,
        CommittedOperation(),
        {
            "status": "committed",
            "metadataDigest": "0" * 64,
            "entryId": "native",
            "revision": "r",
            "leafId": "leaf",
        },
        publication=True,
    )
    pending = journal.publications.pending(session)
    assert len(pending) == 1
    original_fsync = os.fsync

    def deny_directory(fd):
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
        raise OSError("post-commit directory sync fault")

    monkeypatch.setattr(os, "fsync", deny_directory)
    with pytest.raises(CompactionJournalUnknownError, match="UNKNOWN"):
        journal.publications.observe(commit_id, pending[0].metadata_json)
    monkeypatch.setattr(os, "fsync", original_fsync)
    # No rollback is promised once SQLite COMMIT returns. Reconcile exact ID
    # without sending the native operation or projection a second time.
    reopened = CompactionJournal(journal.path)
    assert reopened.publications.pending(session) == ()


def test_changed_publication_metadata_refuses_local_projection(journal):
    journal, session = journal
    commit_id = journal.operations.begin(
        session,
        {},
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    journal.operations.resolve(
        commit_id,
        CommittedOperation(),
        {
            "status": "committed",
            "metadataDigest": "0" * 64,
            "entryId": "native",
            "revision": "r",
            "leafId": "leaf",
        },
        publication=True,
    )
    with sqlite3.connect(journal.path) as db:
        db.execute(
            "UPDATE publications SET metadata_json = ? WHERE commit_id = ?",
            (json.dumps({"commitId": commit_id, "summary": "forged leak"}), commit_id),
        )
    with pytest.raises(CompactionJournalError, match="metadata changed"):
        journal.publications.pending(session)


def test_no_publication_without_exact_committed_native_evidence(journal):
    journal, session = journal
    commit_id = journal.operations.begin(
        session,
        {},
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    with pytest.raises(CompactionJournalError, match="Exact committed"):
        journal.operations.resolve(
            commit_id,
            CommittedOperation(),
            {"status": "committed", "metadataDigest": "0" * 64, "entryId": "x"},
            publication=True,
        )
    assert journal.operations.get(commit_id).state.declared_name == "intent"
    journal.operations.resolve(commit_id, AbortedNoWriteOperation(), {"status": "aborted-no-write"})
    assert journal.publications.pending(session) == ()


def test_session_alias_cannot_bypass_unresolved_intent(journal, tmp_path):
    journal, session = journal
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to(session)
    journal.operations.begin(
        session,
        {},
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    with pytest.raises(CompactionJournalError):
        journal.operations.begin(
            str(alias),
            {},
            inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
        )


def test_durable_intent_does_not_retain_mutable_caller_data(journal):
    journal, session = journal
    intent = {"witness": {"leaf": "original"}}
    commit_id = journal.operations.begin(
        session,
        intent,
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    intent["witness"]["leaf"] = "forged"
    assert json.loads(journal.operations.get(commit_id).intent_json)["witness"]["leaf"] == "original"
