"""Saved journal formats and strict native receipts survive owner replacement."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from agent_comms.child_process import ExitedOutcome
from agent_comms.compaction_source import CompactionSource
from agent_comms.compaction_states import (
    CommittedOperation,
    NativeOutcome,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_compaction_prepare import NativeWitness


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
    from agent_comms.retained_task_facts import RetainedTaskFacts

    source = CompactionSource(witness, "/wire:1:2", "owner", 3, "turn", None, None, "inputs", RetainedTaskFacts(()), ())
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
        "input_revision",
        "retained",
        "pending_inputs",
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
    outcome = FieldCodec.decode(NativeOutcome, wire).checked_child(ExitedOutcome(0))
    assert outcome.state == CommittedOperation()
    assert FieldCodec.encode(outcome) == wire
    assert outcome.bind_metadata("a" * 64) is outcome
    mismatch = outcome.bind_metadata("b" * 64)
    assert not mismatch.state.terminal
    assert FieldCodec.encode(mismatch) == {
        "status": "unknown",
        "reason": "native-metadata-mismatch",
    }
    for invalid in (
        {**wire, "extra": "ignored?"},
        {**wire, "metadataDigest": "bad"},
        {"status": "intent"},
        {"status": "refused"},
        {"status": "unknown", "reason": ""},
    ):
        with pytest.raises(ValueError):
            FieldCodec.decode(NativeOutcome, invalid).checked_child(ExitedOutcome(0))
    with pytest.raises(ValueError, match="Inconsistent"):
        FieldCodec.decode(NativeOutcome, wire).checked_child(ExitedOutcome(1))
    assert (
        FieldCodec.decode(NativeOutcome, {"status": "unknown", "reason": "402"})
        .checked_child(ExitedOutcome(1))
        .reason
        == "402"
    )


@pytest.fixture
def original_covered_cut(tmp_path):
    from agent_comms.compaction_records import CompactionOperation
    from agent_comms.compaction_states import CommittedNativeOutcome
    from agent_comms.native_compaction_request import NativeIntent, NativeSummaryPayload
    from agent_comms.native_entries import ManagedCompactionEntry, NativeEntry
    from agent_comms.pi_summary_payloads import ManagedSummaryMetadata
    from agent_comms.private_path import FileRevision

    sessions = tmp_path / "sessions"
    sessions.mkdir(mode=0o700)
    file = sessions / "original.jsonl"
    rows = [
        {"type": "session", "id": "original", "version": 3},
        {"type": "label", "id": "replaced", "parentId": None},
        {"type": "label", "id": "kept", "parentId": "replaced"},
        {"type": "label", "id": "prepared", "parentId": "kept"},
    ]
    file.write_text("".join(json.dumps(row) + "\n" for row in rows))
    file.chmod(0o600)
    witness = NativeWitness("original", str(file), "prepared", "kept",
                            FileRevision.from_stat(file.stat()))
    payload = NativeSummaryPayload(summary="Original replacement", tokens_before=12)
    intent = NativeIntent(witness, payload.payload_digest(witness), payload.metadata_digest())
    entry = ManagedCompactionEntry(id="committed", parent_id="prepared",
        summary=payload.summary, tokens_before=payload.tokens_before, first_kept_entry_id="kept",
        details=ManagedSummaryMetadata(agent_comms_commit=intent.identity("commit")))
    with file.open("a") as output:
        output.write(json.dumps(entry.to_wire()) + "\n")
    outcome = CommittedNativeOutcome("committed", FileRevision.from_stat(file.stat()),
                                    "committed", intent.metadata_digest)
    operation = CompactionOperation("commit", str(file), json.dumps(FieldCodec.encode(intent)),
                                   CommittedOperation(), outcome.journal_json())
    with NativeEntry.open_evidence(file) as evidence:
        evidence.observe()
        yield file, intent, outcome, operation, entry, evidence


def test_committed_coverage_preserves_original_cut_and_corruption_refusal(original_covered_cut):
    from agent_comms.compaction_errors import CompactionJournalError
    from agent_comms.compaction_identity import NativeCommitIdentity

    file, intent, outcome, operation, entry, evidence = original_covered_cut
    branch = evidence.branch(entry.id, evidence.entries)
    assert operation.covered_prefix(entry, evidence, branch) == frozenset({"replaced"})
    # A later append is allowed without replacing either recorded position.
    with file.open("a") as output:
        output.write(json.dumps({"type": "label", "id": "later", "parentId": entry.id}) + "\n")
    evidence.observe()
    assert operation.covered_prefix(entry, evidence, branch) == frozenset({"replaced"})

    wrong_witnesses = (
        replace(intent.witness, session_id="another"),
        replace(intent.witness, session_file=str(file.with_name("another.jsonl"))),
        replace(intent.witness, revision=replace(intent.witness.revision,
            identity=replace(intent.witness.revision.identity, inode=0))),
        replace(intent.witness, leaf_id="another"),
        replace(intent.witness, first_kept_entry_id="replaced"),
    )
    for witness in wrong_witnesses:
        changed = replace(operation, intent_json=json.dumps(FieldCodec.encode(replace(intent, witness=witness))))
        with pytest.raises(CompactionJournalError):
            changed.covered_prefix(entry, evidence, branch)
    for returned in (
        replace(outcome, entry_id="another"),
        replace(outcome, revision=replace(outcome.revision,
            identity=replace(outcome.revision.identity, inode=0))),
        replace(outcome, revision=replace(outcome.revision, size=evidence.source.size + 1)),
        replace(outcome, metadata_digest="a" * 64),
    ):
        with pytest.raises(CompactionJournalError):
            replace(operation, evidence_json=returned.journal_json()).covered_prefix(entry, evidence, branch)
    for changed in (
        replace(entry, summary="Changed replacement"),
        replace(entry, details=replace(entry.details,
            agent_comms_commit=NativeCommitIdentity("another", intent.payload_digest, intent.metadata_digest))),
    ):
        with pytest.raises(CompactionJournalError):
            operation.covered_prefix(changed, evidence, branch)


def test_sdk_fork_coverage_requires_original_creation_bytes(original_covered_cut):
    from agent_comms.compaction_records import NativeForkCreation
    from agent_comms.compaction_errors import CompactionJournalError
    from agent_comms.native_entries import NativeEntry
    from agent_comms.native_pi import NativePiUnavailable
    from agent_comms.native_session_reopen import NativeSessionIdentity
    from agent_comms.private_path import FileRevision
    from agent_comms.text_digest import TextDigest

    parent, _, _, _, _, _ = original_covered_cut
    child = parent.with_name("fork.jsonl")
    rows = [
        {"type": "session", "id": "fork", "version": 3, "parentSession": str(parent)},
        {"type": "label", "id": "inherited", "parentId": None},
    ]
    raw = "".join(json.dumps(row) + "\n" for row in rows)
    child.write_text(raw)
    child.chmod(0o600)
    creation = NativeForkCreation("fork", str(child), NativeSessionIdentity("original", str(parent)),
        FileRevision.from_stat(parent.stat()), FileRevision.from_stat(child.stat()), TextDigest.of(raw), 2)
    with child.open("a") as output:
        output.write(json.dumps({"type": "label", "id": "new", "parentId": "inherited"}) + "\n")
    with NativeEntry.open_evidence(child) as evidence:
        _, entries = evidence.observe()
        assert creation.covered_prefix(evidence, entries) == frozenset({"fork", "inherited"})
        for changed in (
            replace(creation, session_id="another"),
            replace(creation, source=NativeSessionIdentity("original", str(child.with_name("other.jsonl")))),
            replace(creation, revision=replace(creation.revision,
                identity=replace(creation.revision.identity, inode=0))),
            replace(creation, entry_count=len(entries) + 1),
        ):
            with pytest.raises(CompactionJournalError):
                changed.covered_prefix(evidence, entries)
        with pytest.raises(NativePiUnavailable, match="prefix changed"):
            replace(creation, prefix_digest=TextDigest.of("not the original prefix")).covered_prefix(evidence, entries)
