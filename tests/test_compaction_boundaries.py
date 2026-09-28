"""Saved journal formats and strict native receipts survive owner replacement."""

import json
import sqlite3
from dataclasses import FrozenInstanceError

import pytest

from agent_comms.compaction_journal import CompactionJournal, CompactionJournalError
from agent_comms.compaction_states import (
    CommittedOperation,
    DeclinedPrestartSummary,
    LinkedSummary,
    NativeOutcome,
    OperationState,
    PublicationState,
    SummaryState,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_compaction_commit import CompactionSource
from agent_comms.owner_compaction_prepare import NativeWitness


def test_lifecycle_spelling_and_transitions_match_durable_contract():
    assert OperationState.transition_table() == {
        "intent": {"unknown", "committed", "refused", "aborted-no-write"},
        "unknown": {"unknown", "committed", "aborted-no-write"},
        "committed": set(),
        "refused": set(),
        "aborted-no-write": set(),
    }
    assert SummaryState.transition_table() == {
        "reserved": {"unknown", "linked", "declined-prestart"},
        "unknown": {"unknown"},
        "linked": set(),
        "declined-prestart": set(),
    }
    assert PublicationState.transition_table() == {"pending": {"observed"}, "observed": set()}


def test_reopen_original_sql_rows_without_rewriting_evidence(tmp_path):
    path = tmp_path / "compaction-commits.sqlite3"
    # Literal old schema/rows: this is a saved-data fixture, not generated from
    # the new declarations. No new registry/leases/native attempts are created.
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE operations(commit_id TEXT PRIMARY KEY, session_file TEXT NOT NULL,
                intent_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN
                ('intent','unknown','committed','refused','aborted-no-write')), evidence_json TEXT);
            CREATE TABLE publications(commit_id TEXT PRIMARY KEY, session_file TEXT NOT NULL,
                metadata_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','observed')));
            CREATE TABLE selected_summary_attempts(operation_id TEXT PRIMARY KEY,
                session_file TEXT NOT NULL, source_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('reserved','unknown','linked','declined-prestart')),
                commit_id TEXT, decline_reason TEXT);
        """)
        for number, name in enumerate(
            ("intent", "unknown", "committed", "refused", "aborted-no-write")
        ):
            db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?)",
                (
                    str(number),
                    str(tmp_path / name),
                    '{ "source": "retained" }',
                    name,
                    '{ "receipt": "unchanged" }' if number else None,
                ),
            )
        for number, name in enumerate(("reserved", "unknown", "linked", "declined-prestart")):
            db.execute(
                "INSERT INTO selected_summary_attempts VALUES (?,?,?,?,?,?)",
                (
                    str(number),
                    str(tmp_path / name),
                    '{ "input": "never replay" }',
                    name,
                    "2" if name == "linked" else None,
                    "split_turn" if name == "declined-prestart" else None,
                ),
            )
        for name in ("pending", "observed"):
            db.execute(
                "INSERT INTO publications VALUES (?,?,?,?)",
                (name, "/saved", '{ "commitId": "retained" }', name),
            )
        before = {
            table: db.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("operations", "selected_summary_attempts", "publications")
        }
    journal = CompactionJournal(path)
    for number, row in enumerate(before["operations"]):
        operation = journal.get(str(number))
        assert operation.state.declared_name == row[3]
        assert (operation.intent_json, operation.evidence_json) == (row[2], row[4])
    for number, row in enumerate(before["selected_summary_attempts"]):
        attempt = journal.selected_summary(str(number))
        assert attempt.state.declared_name == row[3]
        assert attempt.source_json == row[2]
    assert journal.selected_summary("2").state == LinkedSummary("2")
    assert journal.selected_summary("3").state == DeclinedPrestartSummary("split_turn")
    with sqlite3.connect(path) as db:
        assert {
            table: db.execute(f"SELECT * FROM {table}").fetchall() for table in before
        } == before
        # A terminal tag with missing evidence cannot masquerade as a typed state.
        db.execute("UPDATE selected_summary_attempts SET commit_id = NULL WHERE operation_id='2'")
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.selected_summary("2")


def native_wire():
    return dict(
        sessionId="saved",
        sessionFile="/saved/chat.jsonl",
        leafId="leaf",
        firstKeptEntryId="kept",
        revision="1:2:3:4:5",
    )


def test_witness_has_one_owner_and_preserves_external_source_json():
    wire = native_wire()
    witness = FieldCodec.decode(NativeWitness, wire)
    assert FieldCodec.encode(witness) == wire
    wire["leafId"] = "mutated after decode"
    assert witness.leaf_id == "leaf"
    with pytest.raises(FrozenInstanceError):
        witness.leaf_id = "changed"
    source = CompactionSource(witness, "/wire:1:2", "owner", 3, "turn", None, None, "bus", "inputs")
    encoded = FieldCodec.project(source, "journal")
    assert "native" not in encoded
    assert encoded["native_json"] == json.dumps(
        native_wire(), sort_keys=True, separators=(",", ":")
    )
    assert set(encoded) == {
        "native_json",
        "wire_root",
        "thread",
        "owner_epoch",
        "turn_id",
        "goal_id",
        "goal_revision",
        "bus_revision",
        "input_revision",
        "pending_input_key",
        "settings_paths",
        "settings_revision",
    }


@pytest.mark.parametrize("change", ["missing", "extra", "empty", "bool", "relative", "revision"])
def test_native_witness_rejects_malformed_boundary_once(change):
    wire = native_wire()
    if change == "missing":
        del wire["leafId"]
    elif change == "extra":
        wire["extension"] = "unowned"
    elif change == "empty":
        wire["leafId"] = ""
    elif change == "bool":
        wire["sessionId"] = True
    elif change == "relative":
        wire["sessionFile"] = "relative.jsonl"
    else:
        wire["revision"] = "not a native revision"
    with pytest.raises((TypeError, ValueError)):
        FieldCodec.decode(NativeWitness, wire)


def test_native_outcome_receipts_and_metadata_refusal():
    wire = dict(
        status="committed",
        entryId="entry",
        leafId="leaf",
        revision="1:2:3:4:5",
        metadataDigest="a" * 64,
    )
    outcome = NativeOutcome.from_wire(wire, 0)
    assert outcome.state == CommittedOperation()
    assert outcome.evidence == wire
    assert outcome.bind_metadata("a" * 64) is outcome
    mismatch = outcome.bind_metadata("b" * 64)
    assert not mismatch.state.terminal
    assert mismatch.evidence == {"status": "unknown", "reason": "native-metadata-mismatch"}
    for invalid in (
        {**wire, "extra": "ignored?"},
        {**wire, "metadataDigest": "bad"},
        {"status": "intent"},
        {"status": "refused"},
        {"status": "unknown", "reason": ""},
    ):
        with pytest.raises(ValueError):
            NativeOutcome.from_wire(invalid, 0)
    with pytest.raises(ValueError, match="Inconsistent"):
        NativeOutcome.from_wire(wire, 1)
    assert (
        NativeOutcome.from_wire({"status": "unknown", "reason": "402"}, 1).evidence["reason"]
        == "402"
    )
