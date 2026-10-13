"""The one-shot carry preserves history and certifies a separate candidate."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.private_bus_checkpoint import CheckpointNeedsRepair, install_private_bus_checkpoint
from agent_comms.bus_source_page import AddressedPage
from agent_comms.turn_context import (
    ContextManifest, ContextManifestWire, JournalProvenance, NativeProvenance,
    RecordedContextTurn, SegmentManifest, TranscriptSegment,
)
from agent_comms.thread_identity import TurnId, TurnIdentity
from agent_comms.wire_log import WireLog
from agent_comms.wire_record import WireRecord
from test_private_bus_checkpoint import _root
from agent_comms.bus_publication import stable_thread_lookup
from tools.cutover.carry_context_manifest_rows import (
    ManifestRowCarry, OriginalManifestObservation, OriginalManifestRow,
)


def original_root(tmp_path):
    comms, root_id = _root(tmp_path)
    first = comms.messaging.send_initial_cohort("sender", "#team", "@Alice first")
    owner = comms.registry.require("Alice")
    identity = NativeSessionIdentity("original-session", "/not-readable/session.jsonl")
    native = NativeProvenance(identity, 1, "a" * 64)
    contributors = tuple(SegmentManifest(
        TranscriptSegment, (native, JournalProvenance(identity.session_file, (f"entry-{i}",))),
        hashlib.sha256(f"original-{i}".encode()).hexdigest(), 10, 3,
    ) for i in range(20))
    manifest = ContextManifest(owner.incarnation,
        RecordedContextTurn(TurnId("original-turn"), TurnIdentity(owner.incarnation, 1)),
        (SegmentManifest(TranscriptSegment, (native,), "b" * 64, 100, 30,
                         contributors, captured_text=("x" * 8000,)),),
        "pi.estimateTokens", request_id="original-request")
    comms.bus.log.record_context(manifest)
    second = comms.messaging.send_initial_cohort("sender", "#team", "@Alice second")
    raw_rows = comms.bus.log.path.read_bytes().splitlines(keepends=True)
    original = []
    for raw in raw_rows:
        record = WireRecord.from_wire(json.loads(raw), root_id)
        if record.context_manifests():
            raw = json.dumps(FieldCodec.encode(OriginalManifestRow(
                OriginalManifestObservation("context_manifest", manifest)))).encode() + b"\n"
        original.append(raw)
    # Only this synthetic fixture uses the original full-form reader while
    # the canonical installer certifies it. The reader is then restored.
    log = comms.bus.log
    marker = replace(log.read_metadata_unlocked(), checkpoint_version=None, checkpoint_seal=None)
    log.path.write_bytes(b"".join(original))
    log.write_metadata_unlocked(marker)
    (comms.root / "private_bus_checkpoint.sqlite3").unlink()
    with patch.object(ContextManifestWire, "from_wire",
                      side_effect=lambda data: FieldCodec.decode(ContextManifest, data)):
        install_private_bus_checkpoint(log)
    return comms, root_id, manifest, (first, second)


def hashes(root):
    result = {}
    for name in ("bus.jsonl", "bus_meta.json", "private_bus_checkpoint.sqlite3"):
        with (root / name).open("rb") as stream:
            result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def test_carry_preserves_history_and_rebuilds_sealed_offsets(tmp_path):
    comms, root_id, original, messages = original_root(tmp_path)
    before = hashes(comms.root)
    candidate = tmp_path / "candidate"
    result = ManifestRowCarry(comms.root, candidate, root_id).run()
    assert hashes(comms.root) == before
    assert result["rows"] == 3 and result["manifests"] == 1
    assert result["published"] is False and result["source_unchanged"] is True
    assert result["candidate_bytes"] < result["source_bytes"]
    reopened = WireLog(candidate / "bus.jsonl")
    assert reopened.full_history() == list(messages)
    with reopened.certified_read() as source:
        assert tuple(source.context_manifests(original.thread, comms.registry.snapshot())) == (original,)
        assert source.marker.last_seq == messages[-1].seq
        source.marker.seal.check_final(source.witness, candidate / "private_bus_checkpoint.sqlite3")
        source.require_current()
    with reopened.certified_read() as source:
        request = AddressedPage.capture(lookup=stable_thread_lookup(17002.0), after_seq=0, limit=100)
        _, delivered, _ = source.addressed_page(reopened, request)
        assert tuple(item.message for item in delivered) == messages
    assert result["source_certificate"]["digest"] != result["candidate_certificate"]["digest"]
    assert json.loads((candidate / "carry-result.json").read_text()) == result
    # Actual command boundary, still entirely on isolated roots.
    completed = subprocess.run([
        sys.executable, str(Path(__file__).resolve().parents[1] / "tools/cutover/carry_context_manifest_rows.py"),
        "--source", str(comms.root), "--candidate", str(tmp_path / "cli-candidate"),
        "--root-id", root_id,
    ], capture_output=True, text=True, env={key: value for key, value in os.environ.items()
        if key not in {"AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID", "AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE"}}, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["history_preserved"] is True
    assert hashes(comms.root) == before


def test_carry_refuses_wrong_identity_or_existing_candidate(tmp_path):
    comms, root_id, _, _ = original_root(tmp_path)
    before = hashes(comms.root)
    candidate = tmp_path / "candidate"
    redirected = tmp_path / "redirected-wire"
    redirected.symlink_to(comms.root, target_is_directory=True)
    with pytest.raises(ValueError, match="ancestry"):
        ManifestRowCarry(redirected, candidate, root_id).run()
    with pytest.raises(ValueError, match="separate"):
        ManifestRowCarry(comms.root, comms.root / "candidate", root_id).run()
    with pytest.raises(ValueError, match="root identity"):
        ManifestRowCarry(comms.root, candidate, "0" * 32).run()
    assert not candidate.exists()
    candidate.mkdir()
    retained = candidate / "retained"
    retained.write_text("keep")
    with pytest.raises(FileExistsError, match="fresh"):
        ManifestRowCarry(comms.root, candidate, root_id).run()
    assert retained.read_text() == "keep" and hashes(comms.root) == before


def test_carry_refuses_changed_original_prefix_without_repair(tmp_path):
    comms, root_id, _, _ = original_root(tmp_path)
    raw = comms.bus.log.path.read_bytes()
    changed = raw.replace(b"entry-0", b"entry-x", 1)
    assert changed != raw and len(changed) == len(raw)
    comms.bus.log.path.write_bytes(changed)
    before = hashes(comms.root)
    with pytest.raises(CheckpointNeedsRepair, match="does not cover"):
        ManifestRowCarry(comms.root, tmp_path / "candidate", root_id).run()
    assert hashes(comms.root) == before


def test_carry_does_not_admit_incomplete_or_unknown_observations():
    with pytest.raises(RelationViolationError, match="incomplete"):
        ManifestRowCarry.convert(b"{}")
    with pytest.raises(ValueError):
        ManifestRowCarry.convert(b'{"observation":{"kind":"unknown","manifest":{}}}\n')
