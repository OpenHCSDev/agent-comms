"""Actual marker/seal persistence contract and every writer-seal fsync seam."""

import copy
import json

import pytest

from agent_comms.checkpoint_seals import CheckpointSeal, FinalSeal, PendingSeal
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.wire_log import WireLog
from test_private_bus_checkpoint import _page, _root, stable_thread_lookup


@pytest.mark.parametrize("mode", ["public", "private", "claims", "final", "pending"])
def test_current_saved_protocol_roundtrips_without_migration(tmp_path, mode):
    comms, root_id = _root(tmp_path)
    raw = {"last_seq": 0}
    if mode != "public":
        raw.update(writer_protocol_version=1, wire_root_id=root_id)
    if mode in ("claims", "final", "pending"):
        raw["claim_envelopes_version"] = 1
    if mode in ("final", "pending"):
        witness = install_private_bus_checkpoint(comms.bus.log)
        seal = (
            FinalSeal.capture(witness, comms.root / "private_bus_checkpoint.sqlite3")
            if mode == "final"
            else PendingSeal.capture(
                witness, witness, comms.root / "private_bus_checkpoint.sqlite3"
            )
        )
        raw.update(checkpoint_version=1, checkpoint_seal=FieldCodec.encode(seal))
        assert (
            FieldCodec.encode(FieldCodec.decode(CheckpointSeal, raw["checkpoint_seal"]))
            == raw["checkpoint_seal"]
        )
    # Decode actual bytes, without invoking a bus read on a deliberately changed protocol.
    comms.bus.log.metadata_path.write_text(json.dumps(raw))
    marker = comms.bus.log.read_metadata_unlocked()
    assert FieldCodec.encode(marker) == raw
    assert marker.private is (mode != "public")
    assert marker.claims is (mode in ("claims", "final", "pending"))


@pytest.mark.parametrize(
    "damage",
    [
        "extra",
        "null-root",
        "missing-seq",
        "bool-seq",
        "negative-seq",
        "huge-seq",
        "bad-root",
        "writer-bool",
        "claim-bool",
        "no-claims",
        "no-checkpoint-version",
        "no-seal",
        "seal-extra",
        "seal-state",
        "seal-version-bool",
        "seal-revision-bool",
        "seal-digest",
        "seal-witness-extra",
        "seal-missing-witness",
    ],
)
def test_malformed_marker_denied_by_wire_and_registry_same_boundary(tmp_path, damage):
    comms, _ = _root(tmp_path)
    install_private_bus_checkpoint(comms.bus.log)
    original = json.loads(comms.bus.log.metadata_path.read_text())
    raw = copy.deepcopy(original)
    seal = raw["checkpoint_seal"]
    if damage == "extra":
        raw["foreign"] = 1
    elif damage == "null-root":
        raw["wire_root_id"] = None
    elif damage == "missing-seq":
        del raw["last_seq"]
    elif damage == "bool-seq":
        raw["last_seq"] = True
    elif damage == "negative-seq":
        raw["last_seq"] = -1
    elif damage == "huge-seq":
        raw["last_seq"] = 1 << 63
    elif damage == "bad-root":
        raw["wire_root_id"] = "z" * 32
    elif damage == "writer-bool":
        raw["writer_protocol_version"] = True
    elif damage == "claim-bool":
        raw["claim_envelopes_version"] = True
    elif damage == "no-claims":
        del raw["claim_envelopes_version"]
    elif damage == "no-checkpoint-version":
        del raw["checkpoint_version"]
    elif damage == "no-seal":
        del raw["checkpoint_seal"]
    elif damage == "seal-extra":
        seal["foreign"] = 1
    elif damage == "seal-state":
        seal["state"] = "unknown"
    elif damage == "seal-version-bool":
        seal["version"] = True
    elif damage == "seal-revision-bool":
        seal["db_revision"][0] = True
    elif damage == "seal-digest":
        seal["witness"]["digest"] = "z" * 64
    elif damage == "seal-witness-extra":
        seal["witness"]["latest_initial_seq"] = 999
    elif damage == "seal-missing-witness":
        del seal["witness"]
    comms.bus.log.metadata_path.write_text(json.dumps(raw))
    with pytest.raises(RelationViolationError):
        comms.bus.log._private_marker_unlocked()
    with pytest.raises(RelationViolationError):
        comms.registry.store.private_guard_unlocked()
    assert (
        json.loads(comms.bus.log.metadata_path.read_text()) == raw
    )  # No repair from shape failure.


@pytest.mark.parametrize(
    "stage", ["pending-before", "pending-after", "final-before", "final-after"]
)
def test_uncertain_seal_publication_recovers_exact_committed_suffix(tmp_path, monkeypatch, stage):
    comms, _ = _root(tmp_path)
    install_private_bus_checkpoint(comms.bus.log)
    comms.messaging.send_initial_cohort("sender", "Alice", "original")
    real = WireLog.write_metadata_unlocked
    armed = True

    def write(self, marker):
        nonlocal armed
        matched = (
            isinstance(marker.seal, PendingSeal)
            if stage.startswith("pending")
            else isinstance(marker.seal, FinalSeal) and marker.seal.witness.through_seq == 2
        )
        if armed and matched:
            armed = False
            if stage.endswith("after"):
                real(self, marker)
            raise OSError("uncertain seal durability")
        return real(self, marker)

    with monkeypatch.context() as patch:
        patch.setattr(WireLog, "write_metadata_unlocked", write)
        with pytest.raises(RelationViolationError, match="UNKNOWN"):
            comms.messaging.send_initial_cohort("sender", "Alice", "durable bus, uncertain seal")
    bus = comms.bus.log.path.read_bytes()
    witness, rows, more = _page(comms, stable_thread_lookup(17002.0))
    assert [row.message.seq for row in rows] == [1, 2] and not more
    assert witness.through_seq == 2
    assert comms.bus.log.path.read_bytes() == bus
    assert isinstance(comms.bus.log._private_marker_unlocked().seal, FinalSeal)
    # Reopened canonical readers agree; no replay or new publication.
    assert [row.message.seq for row in _page(comms, stable_thread_lookup(17002.0))[1]] == [1, 2]


def test_absent_public_marker_never_substitutes_for_required_or_redirected_marker(tmp_path):
    wire = WireLog(tmp_path / "bus.jsonl")
    assert wire.read_metadata_unlocked().last_seq == 0
    with pytest.raises(RelationViolationError, match="missing"):
        wire.read_metadata_unlocked(required=True)
    wire.metadata_path.symlink_to(tmp_path / "missing-target")
    with pytest.raises(RelationViolationError, match="redirected"):
        wire.read_metadata_unlocked()


def test_registry_guard_does_not_treat_dangling_marker_as_public(tmp_path):
    from agent_comms.registry_store import RegistryStore

    (tmp_path / "bus_meta.json").symlink_to(tmp_path / "missing-target")
    with pytest.raises(RelationViolationError):
        RegistryStore(tmp_path / "registry.json").private_guard_unlocked()
