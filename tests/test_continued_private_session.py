"""Continued private history must join every user to independent live evidence."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from native_proof_cases import read_proof_rows, write_proof_rows

import pytest

from agent_comms.native_input_record import TriageNativeExecution, FullNativeExecution
from agent_comms.selected_triage import IgnoreSelectedTriage
from agent_comms.compaction_errors import CompactionJournalError
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import SelectedSummarySource
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions
from agent_comms.private_sidecar import native_request_digest
from agent_comms.owner_compaction_settings import PiCompactionSettings
from agent_comms.pi_summary_payloads import SelectedModel
from agent_comms.retained_task_facts import InputTaskFact, RetainedTaskFacts
from agent_comms.selected_source import SessionRevision
from selected_summary_cases import admission_identity


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
    source = SelectedSummarySource(
        admission_identity(
            session, text="new", key="acp:new", turn="new-turn", owner="owner", admission=2
        ).source,
        selected=SelectedModel("fixture", "fixture", 1000),
        settings=PiCompactionSettings(100, 10),
        retained=RetainedTaskFacts((InputTaskFact(inputs.read().rows["acp:new"]),)),
    )
    return CompactionJournal(tmp_path / "compaction-commits.sqlite3"), session, inputs, source


def test_continued_private_session_needs_no_fresh_object_and_preserves_history(continued):
    journal, session, inputs, source = continued
    before = session.read_bytes(), inputs.path.read_bytes()
    operation = journal.summaries.reserve(str(session), source)
    assert journal.summaries.get(operation).state.declared_name == "reserved"
    assert before == (session.read_bytes(), inputs.path.read_bytes())
    with pytest.raises(CompactionJournalError):
        journal.summaries.reserve(str(session), source)


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
        journal.private_inputs.reserve(session, "b" * 32)
    inputs.path.write_text(json.dumps(saved))
    session.write_text("".join(json.dumps(row) + "\n" for row in entries))
    if damage != "revision":
        source = replace(source, source=replace(source.source,
            reserved_revision=SessionRevision.observe(str(session)).require_available()))
    with pytest.raises((CompactionJournalError, ValueError)):
        journal.summaries.reserve(str(session), source)
    assert journal.summaries.unresolved(str(session)) == ()
    assert json.loads(inputs.path.read_text())["rows"] == rows


@pytest.mark.parametrize(
    "damage", [None, "context", "unsettled", "foreign", "no-marker", "extended", "null", "opaque"]
)
def test_live_recorded_raw_context_covers_marker_without_erasing_unknown(continued, damage):
    from agent_comms.assignment_states import TriagePendingAssignment
    from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
    from agent_comms.coordination_tables.assignments import MessageAudience, WakeAssignment
    from agent_comms.coordinator import Coordination
    from agent_comms.native_runtime_input import NativeRuntimeInput
    from agent_comms.native_admission_epoch import RecordedNativeAdmission

    journal, session, inputs, source = continued
    inputs.update(lambda document: replace(document, rows={"acp:new": document.rows["acp:new"]}))
    if damage != "no-marker":
        journal.private_inputs.reserve(session, "a" * 32)
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
    write_proof_rows(session, [proof])
    # Native schema fixture: the immutable result columns represent an already
    # recorded live result; the journal must only corroborate those columns.
    with Coordination(str(journal.path.parent / "coordination.sqlite3")) as store:
        store.participants.register("f" * 32, "owner", "owner", committed=True)
        store.assignments.accept(
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
        with store.session.transaction() as db:
            NativeRuntimeInput(
                input_id="a" * 32,
                stage=TriageNativeExecution, execution_id=None, attempt_ordinal=None,
                owner_lookup="f" * 32,
                owner_thread="owner",
                owner_generation=1,
                owner_token_digest="c" * 64,
                sent_owner_admission_generation=RecordedNativeAdmission(1),
                session_id=None if damage == "unsettled" else ("foreign-session" if damage == "foreign" else "session"),
                session_file=None if damage == "unsettled" else str(session),
                session_entry_id=None if damage == "unsettled" else "user",
                request_generation=None if damage == "unsettled" else 1,
                llm_context_digest=(
                    None if damage == "unsettled" else ("d" if damage == "context" else "b") * 64
                ),
                verdict=IgnoreSelectedTriage,
            ).insert(db)
            TriageNativeExecution().record_sources(db, "a" * 32, (store.assignments.get("claim"),))
    source = replace(source, source=replace(source.source,
        reserved_revision=SessionRevision.observe(str(session)).require_available()))
    if damage in {"context", "unsettled", "foreign"}:
        with pytest.raises(CompactionJournalError, match="coverage floor"):
            journal.summaries.reserve(str(session), source)
    else:
        journal.summaries.reserve(str(session), source)
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT input_id,status FROM private_raw_inputs").fetchall() == (
            [] if damage == "no-marker" else [("a" * 32, "unknown")]
        )


@pytest.mark.parametrize("damage", [None, "missing-operation", "unknown-operation", "summary",
    "cut", "parent", "foreign-file", "untracked-suffix", "raw-unknown", "marker-only"])
def test_original_committed_cut_covers_inherited_prefix_only(continued, damage):
    """A recorded original cut is distinct from file-only or copied coverage."""
    from agent_comms.compaction_records import CompactionOperation
    from agent_comms.compaction_states import CommittedNativeOutcome, CommittedOperation, UnknownOperation
    from agent_comms.native_compaction_request import NativeIntent, NativeSummaryPayload
    from agent_comms.owner_compaction_prepare import NativeWitness
    from agent_comms.pi_summary_payloads import SummaryFiles
    from agent_comms.private_path import FileRevision

    journal, session, inputs, source = continued
    entries = [json.loads(line) for line in session.read_text().splitlines()]
    entries.insert(1, dict(type="message", id="public-user", parentId=None,
        message=dict(role="user", content=[dict(type="text", text="inherited public source")])) )
    entries[2]["parentId"] = "public-user"
    session.write_text("".join(json.dumps(row) + "\n" for row in entries))
    witness = NativeWitness("session", str(session), "user", "user", FileRevision.from_stat(session.stat()))
    payload = NativeSummaryPayload(summary="original summary", tokens_before=100, details=None if damage == "marker-only" else SummaryFiles((), ()))
    intent = NativeIntent(witness, payload.payload_digest(witness), payload.metadata_digest())
    commit = intent.identity("c" * 32)
    entry = dict(type="compaction", id="cut", parentId="user", summary=payload.summary,
        firstKeptEntryId="user", tokensBefore=100,
        details=dict(readFiles=[], modifiedFiles=[], agentCommsCommit=FieldCodec.encode(commit)))
    if damage == "marker-only":
        entry["details"] = dict(agentCommsCommit=FieldCodec.encode(commit))
    entries.append(entry)
    session.write_text("".join(json.dumps(row) + "\n" for row in entries))
    outcome = CommittedNativeOutcome("cut", FileRevision.from_stat(session.stat()), "cut", intent.metadata_digest)
    with journal.transaction() as db:
        CompactionOperation(commit.commit_id, str(session), json.dumps(FieldCodec.encode(intent)),
            UnknownOperation() if damage == "unknown-operation" else CommittedOperation(),
            outcome.journal_json()).insert(db)
        if damage == "missing-operation":
            db.execute("DELETE FROM operations")
    if damage == "summary":
        entry["summary"] = "different"
    if damage == "cut":
        entry["firstKeptEntryId"] = "public-user"
    if damage == "parent":
        entry["parentId"] = "public-user"
    if damage == "untracked-suffix":
        entries.append(dict(type="message", id="untracked", parentId="cut",
            message=dict(role="user", content=[dict(type="text", text="new untracked source")])) )
    session.write_text("".join(json.dumps(row) + "\n" for row in entries))
    if damage == "foreign-file":
        foreign = session.with_name("copied.jsonl")
        foreign.write_bytes(session.read_bytes()); foreign.chmod(0o600)
        session = foreign
    if damage == "raw-unknown":
        journal.private_inputs.reserve(session, "a" * 32)
    source = replace(source, source=replace(source.source,
        reserved_revision=SessionRevision.observe(str(session)).require_available()))
    originals = session.read_bytes(), inputs.path.read_bytes()
    if damage in {None, "marker-only"}:
        journal.summaries.reserve(str(session), source)
    else:
        with pytest.raises(CompactionJournalError, match="coverage floor"):
            journal.summaries.reserve(str(session), source)
        assert journal.summaries.unresolved(str(session)) == ()
    assert originals == (session.read_bytes(), inputs.path.read_bytes())
