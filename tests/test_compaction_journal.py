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
