"""Actual marker/seal persistence contract and every writer-seal fsync seam."""

from conftest import publish_initial
import copy
import json
import shutil

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.checkpoint_seals import FinalSeal, PendingSeal
from agent_comms.errors import RelationViolationError
from agent_comms.wire_log import WireLog
from agent_comms.private_bus_checkpoint import PrefixWitness
from test_private_bus_checkpoint import _page, _root


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
        seal["db_revision"]["size"] = True
    elif damage == "seal-digest":
        seal["witness"]["digest"] = "z" * 64
    elif damage == "seal-witness-extra":
        seal["witness"]["latest_source_seq"] = 999
    elif damage == "seal-missing-witness":
        del seal["witness"]
    comms.bus.log.metadata_path.write_text(json.dumps(raw))
    with pytest.raises(RelationViolationError):
        comms.bus.log.private_marker_unlocked()
    with pytest.raises(RelationViolationError):
        comms.registry.store.private_guard_unlocked()
    assert (
        json.loads(comms.bus.log.metadata_path.read_text()) == raw
    )  # No repair from shape failure.


@pytest.mark.parametrize(
    "stage", ["pending-before", "pending-after", "final-before", "final-after"]
)
@pytest.mark.parametrize("copy_root", [False, True])
def test_uncertain_seal_publication_recovers_exact_committed_suffix(tmp_path, monkeypatch, stage, copy_root):
    comms, _ = _root(tmp_path)
    publish_initial(comms, "sender", "Alice", "original")
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
            publish_initial(comms, "sender", "Alice", "durable bus, uncertain seal")
    if copy_root:
        from agent_comms.comms import Comms

        comms = Comms(shutil.copytree(comms.root, tmp_path / "copied"))
    bus = comms.bus.log.path.read_bytes()
    witness, rows, more = _page(comms, RecordedThreadIncarnation("fixture", 17002.0).lookup)
    assert [row.message.seq for row in rows] == [1, 2] and not more
    assert witness.through_seq == 2
    assert comms.bus.log.path.read_bytes() == bus
    assert isinstance(comms.bus.log.private_marker_unlocked().seal, FinalSeal)
    # Reopened canonical readers agree; no replay or new publication.
    assert [row.message.seq for row in _page(comms, RecordedThreadIncarnation("fixture", 17002.0).lookup)[1]] == [1, 2]


def test_same_inode_interrupted_append_does_not_rescan_prefix(tmp_path, monkeypatch):
    comms, _ = _root(tmp_path)
    publish_initial(comms, "sender", "Alice", "original")
    real = WireLog.write_metadata_unlocked

    def interrupt(self, marker):
        if isinstance(marker.seal, PendingSeal):
            raise OSError("interrupted before index intent")
        return real(self, marker)

    with monkeypatch.context() as patch:
        patch.setattr(WireLog, "write_metadata_unlocked", interrupt)
        with pytest.raises(RelationViolationError, match="UNKNOWN"):
            publish_initial(comms, "sender", "Alice", "interrupted")

    def unexpected_scan(self, bus, info):
        raise AssertionError("same-inode tail recovery rescanned the prefix")

    monkeypatch.setattr(PrefixWitness, "require_prefix", unexpected_scan)
    publish_initial(comms, "sender", "Alice", "new send, not a replay")
    assert _page(comms, RecordedThreadIncarnation("fixture", 17002.0).lookup)[0].through_seq == 3


@pytest.mark.parametrize("corrupt", [False, True])
def test_interrupted_rebind_checks_prefix_before_resuming(tmp_path, monkeypatch, corrupt):
    from agent_comms.comms import Comms

    original, _ = _root(tmp_path)
    publish_initial(original, "sender", "Alice", "prefix-body-" + "x" * 10000)
    comms = Comms(shutil.copytree(original.root, tmp_path / "copied"))
    real = WireLog.write_metadata_unlocked

    def interrupt(self, marker):
        real(self, marker)
        if isinstance(marker.seal, PendingSeal):
            raise OSError("interrupted after rebind intent")

    with monkeypatch.context() as patch:
        patch.setattr(WireLog, "write_metadata_unlocked", interrupt)
        with pytest.raises(RelationViolationError, match="UNKNOWN"):
            with comms.bus.log.locked():
                pass
    assert isinstance(comms.bus.log.private_marker_unlocked().seal, PendingSeal)
    if corrupt:
        raw = comms.bus.log.path.read_bytes()
        comms.bus.log.path.write_bytes(raw.replace(b"prefix-body-", b"broken-body-", 1) + b"\n")
    calls = []
    require_prefix = PrefixWitness.require_prefix

    def checked(self, bus, info):
        calls.append(self.offset)
        return require_prefix(self, bus, info)

    monkeypatch.setattr(PrefixWitness, "require_prefix", checked)
    if corrupt:
        with pytest.raises(RelationViolationError, match="checked prefix changed"):
            with comms.bus.log.locked():
                pass
    else:
        with comms.bus.log.locked():
            pass
        assert isinstance(comms.bus.log.private_marker_unlocked().seal, FinalSeal)
    assert len(calls) == 1


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
