"""Continued private history must join every user to independent live evidence."""

import json
import os
import sqlite3
from dataclasses import replace
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
        "extra-content-field",
        "null-signature",
        "signed-content",
        "opaque-content",
        "string-content",
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
        rows["acp:old"]["kind"] = "bound_unknown"
    if damage == "unbound":
        rows["acp:old"]["native_id"] = None
    if damage == "foreign":
        rows["acp:old"]["owner"] = "someone-else"
    if damage == "untracked":
        del entries[1]["message"]["inputId"]
    if damage == "text":
        entries[1]["message"]["content"][0]["text"] = "different"
    if damage == "extra-content-field":
        entries[1]["message"]["content"][0]["extra"] = True
    if damage == "null-signature":
        entries[1]["message"]["content"][0]["textSignature"] = None
    if damage == "signed-content":
        entries[1]["message"]["content"][0]["textSignature"] = "signature"
    if damage == "opaque-content":
        entries[1]["message"]["content"][0]["type"] = "extension"
    if damage == "string-content":
        entries[1]["message"]["content"] = "old"
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


@pytest.mark.parametrize(
    "damage", [None, "context", "unsettled", "foreign", "no-marker", "extended", "null", "opaque"]
)
def test_live_recorded_raw_context_covers_marker_without_erasing_unknown(continued, damage):
    from agent_comms.assignment_states import TriagePendingAssignment
    from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
    from agent_comms.coordination import MessageAudience, WakeAssignment
    from agent_comms.coordination_store import MutationStore
    from agent_comms.native_runtime_input import NativeRuntimeInput

    journal, session, inputs, source = continued
    inputs.update(lambda document: replace(document, rows={"acp:new": document.rows["acp:new"]}))
    if damage != "no-marker":
        journal.reserve_private_raw_input(session, "a" * 32)
    if damage in {"extended", "null", "opaque"}:
        entries = [json.loads(line) for line in session.read_text().splitlines()]
        part = entries[1]["message"]["content"][0]
        if damage == "extended":
            part["extension"] = {"provenance": "opaque"}
        elif damage == "null":
            part["textSignature"] = None
        else:
            part["type"] = "extension"
        session.write_text("".join(json.dumps(row) + "\n" for row in entries))
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
    with MutationStore(str(journal.path.parent / "coordination.sqlite3")) as store:
        store.register_participant("f" * 32, "owner", "owner", committed=True)
        store.accept_assignment(
            WakeAssignment(
                assignment_id="claim",
                recipient="owner",
                recipient_lookup="f" * 32,
                wire_seq=1,
                message_id="message",
                audience=MessageAudience.COLLECTIVE,
                lifecycle=TriagePendingAssignment(),
                accepted_at_ms=1,
                updated_at_ms=1,
            )
        )
        install_native_runtime_schema(store)
        with store._transaction() as db:
            NativeRuntimeInput(
                input_id="a" * 32,
                stage="triage",
                assignment_id="claim",
                execution_id=None,
                attempt_ordinal=None,
                owner_lookup="f" * 32,
                owner_thread="foreign" if damage == "foreign" else "owner",
                owner_generation=1,
                owner_token_digest="c" * 64,
                sent_owner_admission_generation=1,
                session_id=None if damage == "unsettled" else "session",
                session_file=None if damage == "unsettled" else str(session),
                session_entry_id=None if damage == "unsettled" else "user",
                request_generation=None if damage == "unsettled" else 1,
                llm_context_digest=(
                    None if damage == "unsettled" else ("d" if damage == "context" else "b") * 64
                ),
                verdict="ignore",
            ).insert(db)
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
