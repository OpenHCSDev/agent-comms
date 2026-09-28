"""Saved journal formats and strict native receipts survive owner replacement."""

import json
from dataclasses import FrozenInstanceError

import pytest

from agent_comms.compaction_states import (
    CommittedOperation,
    NativeOutcome,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_compaction_commit import CompactionSource
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
    outcome = FieldCodec.decode(NativeOutcome, wire).checked_exit(0)
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
            FieldCodec.decode(NativeOutcome, invalid).checked_exit(0)
    with pytest.raises(ValueError, match="Inconsistent"):
        FieldCodec.decode(NativeOutcome, wire).checked_exit(1)
    assert (
        FieldCodec.decode(NativeOutcome, {"status": "unknown", "reason": "402"})
        .checked_exit(1)
        .reason
        == "402"
    )
