"""Continued private history must join every user to independent live evidence."""

import json
from dataclasses import replace
import os
import sqlite3
from pathlib import Path

import pytest

from agent_comms.backend import _session_revision
from agent_comms.compaction_journal import CompactionJournal, CompactionJournalError
from agent_comms.input_disposition import InputDispositions
from agent_comms.private_sidecar import native_request_digest


@pytest.fixture
def continued(tmp_path):
    folder = tmp_path / "native-sessions" / ("f" * 32)
    folder.mkdir(parents=True, mode=0o700)
    session = folder / "continued.jsonl"
    native_id = "a" * 32
    entries = [
        dict(type="session", version=3, id="session"),
        dict(
            type="message",
            id="user",
            message=dict(
                role="user",
                inputId=native_id,
                inputDigest=native_request_digest("old"),
                content=[dict(type="text", text="old")],
            ),
        ),
    ]
    session.write_text("".join(json.dumps(row) + "\n" for row in entries))
    session.chmod(0o600)
    inputs = InputDispositions(tmp_path / InputDispositions.filename)
    inputs.record("acp:old", seq=None, owner="owner", admission=1, target="owner", text="old")
    inputs.bind("acp:old", admission=1, turn_id="old-turn", native_id=native_id, text="old")
    inputs.started("acp:old", turn_id="old-turn", native_id=native_id, text="old")
    inputs.record("acp:new", seq=None, owner="owner", admission=2, target="owner", text="new")
    source = dict(
        source=dict(
            ownerName="owner",
            ownerPid=os.getpid(),
            ingressKey="acp:new",
            reservedRevision=json.loads(json.dumps(_session_revision(str(session)))),
        ),
        selected=dict(provider="fixture", modelId="fixture", contextWindow=1000),
        settings=dict(reserveTokens=100, keepRecentTokens=10),
    )
    return CompactionJournal(tmp_path / "compaction-commits.sqlite3"), session, inputs, source


def test_continued_private_session_needs_no_fresh_object_and_preserves_history(continued):
    journal, session, inputs, source = continued
    before = session.read_bytes(), inputs.path.read_bytes()
    operation = journal.reserve_selected_summary(str(session), source)
    assert journal.selected_summary(operation).state.declared_name == "reserved"
    assert before == (session.read_bytes(), inputs.path.read_bytes())
    with pytest.raises(CompactionJournalError):
        journal.reserve_selected_summary(str(session), source)


@pytest.mark.parametrize(
    "damage",
    [
        "unknown",
        "unbound",
        "untracked",
        "text",
        "digest",
        "revision",
        "raw",
        "duplicate",
        "foreign",
    ],
)
def test_continued_private_uncertain_or_mismatched_history_never_reserves(continued, damage):
    journal, session, inputs, source = continued
    saved = json.loads(inputs.path.read_text())
    rows = saved["rows"]
    entries = [json.loads(line) for line in session.read_text().splitlines()]
    if damage == "unknown":
        rows["acp:old"]["status"] = "unknown"
    if damage == "unbound":
        rows["acp:old"]["native_id"] = None
    if damage == "foreign":
        rows["acp:old"]["owner"] = "someone-else"
    if damage == "untracked":
        del entries[1]["message"]["inputId"]
    if damage == "text":
        entries[1]["message"]["content"][0]["text"] = "different"
    if damage == "digest":
        entries[1]["message"]["inputDigest"] = "b" * 64
    if damage == "duplicate":
        entries.append(entries[1])
    if damage == "raw":
        journal.reserve_private_raw_input(session, "b" * 32)
    inputs.path.write_text(json.dumps(saved))
    session.write_text("".join(json.dumps(row) + "\n" for row in entries))
    if damage != "revision":
        source["source"]["reservedRevision"] = json.loads(
            json.dumps(_session_revision(str(session)))
        )
    with pytest.raises((CompactionJournalError, ValueError)):
        journal.reserve_selected_summary(str(session), source)
    assert journal.unresolved_selected_summary(str(session)) == ()
    assert json.loads(inputs.path.read_text())["rows"] == rows


@pytest.mark.parametrize("damage", [None, "context", "unsettled", "foreign", "no-marker"])
def test_live_recorded_raw_context_covers_marker_without_erasing_unknown(continued, damage):
    from agent_comms.coordinated_runtime_schema import _DDL, _DDL_DIGEST

    journal, session, inputs, source = continued
    inputs.update(lambda document: replace(document, rows={"acp:new": document.rows["acp:new"]}))
    if damage != "no-marker":
        journal.reserve_private_raw_input(session, "a" * 32)
    proof = dict(
        schema=1,
        type="context_committed",
        sessionId="session",
        inputId="a" * 32,
        sessionEntryId="user",
        requestGeneration=1,
        llmContextDigest="b" * 64,
    )
    proof_file = Path(str(session) + ".input-proof")
    proof_file.write_text(json.dumps(proof) + "\n")
    proof_file.chmod(0o600)
    # Native schema fixture: the immutable result columns represent an already
    # recorded live result; the journal must only corroborate those columns.
    with sqlite3.connect(journal.path.parent / "coordination.sqlite3") as db:
        for _, ddl in _DDL:
            db.execute(ddl)
        db.execute("INSERT INTO native_runtime_schema_meta VALUES (1,3,?)", (_DDL_DIGEST,))
        db.execute(
            "INSERT INTO native_runtime_inputs (input_id,stage,claim_id,owner_lookup,"
            "owner_thread,owner_generation,owner_token_digest,session_id,session_file,"
            "session_entry_id,request_generation,llm_context_digest,verdict) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "a" * 32,
                "triage",
                "claim",
                "f" * 32,
                "foreign" if damage == "foreign" else "owner",
                1,
                "c" * 64,
                None if damage == "unsettled" else "session",
                None if damage == "unsettled" else str(session),
                None if damage == "unsettled" else "user",
                None if damage == "unsettled" else 1,
                None if damage == "unsettled" else ("d" if damage == "context" else "b") * 64,
                "ignore",
            ),
        )
    (journal.path.parent / "coordination.sqlite3").chmod(0o600)
    source["source"]["reservedRevision"] = json.loads(json.dumps(_session_revision(str(session))))
    if damage in {"context", "unsettled", "foreign"}:
        with pytest.raises(CompactionJournalError, match="coverage floor"):
            journal.reserve_selected_summary(str(session), source)
    else:
        journal.reserve_selected_summary(str(session), source)
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT input_id,status FROM private_raw_inputs").fetchall() == (
            [] if damage == "no-marker" else [("a" * 32, "unknown")]
        )
