"""Durable intent state machine, provider-free and independent of native Pi."""

import json
import os
import subprocess
import sys

import pytest

from agent_comms.compaction_journal import CompactionJournal, CompactionJournalError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX production bridge")


@pytest.fixture
def journal(tmp_path):
    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    return CompactionJournal(tmp_path / "journal.sqlite3"), str(session)


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
