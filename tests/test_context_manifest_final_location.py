"""Quiet final-location installation on an isolated full-form history copy."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.bus_source_page import AddressedPage
from agent_comms.checkpoint_seals import file_revision
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.private_bus_checkpoint import PrefixCertificate, install_private_bus_checkpoint
from agent_comms.turn_context import ContextManifest, ContextManifestWire
from unittest.mock import patch
from agent_comms.wire_log import WireLog
from test_context_manifest_migration import original_root, hashes
from tools.cutover.migrate_context_manifest_rows import ManifestRowMigration


def certified_old_copy(original, destination):
    shutil.copytree(original.root, destination)
    log = WireLog(destination / "bus.jsonl")
    with pytest.raises(RelationViolationError):
        with log.certified_read():
            pytest.fail("Copied certificate must not authorize the new inode")
    # Fixture setup uses the retired decoder to certify this private copy;
    # the product migration never accepts stale/copied certificates as permission.
    marker = replace(log.read_metadata_unlocked(), checkpoint_version=None, checkpoint_seal=None)
    log.write_metadata_unlocked(marker)
    (destination / "private_bus_checkpoint.sqlite3").unlink()
    with patch.object(ContextManifestWire, "from_wire",
                      side_effect=lambda data: FieldCodec.decode(ContextManifest, data)):
        install_private_bus_checkpoint(log)
    return log


def test_final_location_recertifies_migration_and_reads_retained_history(tmp_path):
    original, root_id, manifest, messages = original_root(tmp_path)
    before = hashes(original.root)
    final_root = tmp_path / "final-location"
    final_log = certified_old_copy(original, final_root)
    candidate = tmp_path / "candidate"
    migrated = ManifestRowMigration(original.root, candidate, root_id).run()
    unsealed = replace(final_log.read_metadata_unlocked(), checkpoint_version=None, checkpoint_seal=None)
    candidate_before = hashes(candidate)
    with WireLog(candidate / "bus.jsonl").certified_read() as migrated_source:
        witness = migrated_source.install_at(final_log)
    assert hashes(candidate) == candidate_before
    assert witness.revision == file_revision(final_log.path.stat())
    assert witness.digest == migrated["candidate_certificate"]["digest"]
    assert witness.revision != tuple(migrated["candidate_certificate"]["revision"])
    reopened = Comms(final_root)
    assert reopened.bus.log.full_history() == list(messages)
    assert reopened.bus.log.context_manifests("Alice", reopened.registry) == (manifest,)
    with reopened.bus.log.certified_read() as source:
        source.require_current()
        source.marker.seal.check_final(source.witness, final_root / "private_bus_checkpoint.sqlite3")
        assert replace(source.marker, checkpoint_version=None, checkpoint_seal=None) == unsealed
        request = AddressedPage.capture(lookup=stable_thread_lookup(17002.0), after_seq=0, limit=100)
        _, delivered, _ = source.addressed_page(reopened.bus.log, request)
        assert tuple(item.message for item in delivered) == messages
    installed_hashes = hashes(final_root)
    binary = Path(sys.executable).with_name("agent-comms")
    environment = {key: value for key, value in os.environ.items() if key not in {
        "AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID", "AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE"}}
    results = {}
    for command in (("history", "--everything"), ("context", "Alice", "--turn", "1")):
        completed = subprocess.run([str(binary), "--root", str(final_root), *command],
            capture_output=True, text=True, env=environment, timeout=30)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        results[command[0]] = json.loads(completed.stdout)
    assert results["history"]["everything"] == [
        {**message.to_wire(), **message.display_metadata} for message in messages]
    assert results["context"]["manifests"] == FieldCodec.encode((manifest,))
    assert hashes(original.root) == before
    assert hashes(final_root) == installed_hashes
    (tmp_path / "final-location-result.json").write_text(json.dumps({
        "source_unchanged": True, "final_files_unchanged_by_reads": True,
        "copied_certificate_refused": True, "final_seal_verified": True,
        "retained_messages": len(messages), "retained_manifests": 1,
        "addressed_history_equal": True, "cli_history_equal": True,
        "cli_context_equal": True, "provider_calls": 0,
        "live_root_access": False, "application_binary": str(binary),
        "final_certificate": FieldCodec.encode(witness),
    }, indent=2) + "\n")


def test_failed_install_keeps_required_seal_and_preserves_candidate(tmp_path, monkeypatch):
    original, root_id, _, _ = original_root(tmp_path)
    before = hashes(original.root)
    destination = certified_old_copy(original, tmp_path / "destination")
    candidate = tmp_path / "candidate"
    ManifestRowMigration(original.root, candidate, root_id).run()
    candidate_before = hashes(candidate)
    prior_marker = destination.read_metadata_unlocked()

    def fail_install(cls, bus, marker):
        raise OSError("controlled install failure")

    monkeypatch.setattr(PrefixCertificate, "_install", classmethod(fail_install))
    with WireLog(candidate / "bus.jsonl").certified_read() as source:
        with pytest.raises(RelationViolationError, match="outcome UNKNOWN"):
            source.install_at(destination)
    assert destination.read_metadata_unlocked() == prior_marker
    assert destination.read_metadata_unlocked().requires_checkpoint
    with pytest.raises(RelationViolationError):
        with destination.certified_read():
            pytest.fail("Interrupted migration must not enter unsealed full-scan admission")
    assert hashes(candidate) == candidate_before and hashes(original.root) == before


def test_install_refuses_different_marker_before_publication(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir(mode=0o700)
    right.mkdir(mode=0o700)
    original, root_id, _, _ = original_root(left)
    destination, _, _, _ = original_root(right)
    before = hashes(destination.root)
    candidate = tmp_path / "candidate"
    ManifestRowMigration(original.root, candidate, root_id).run()
    with WireLog(candidate / "bus.jsonl").certified_read() as source:
        with pytest.raises(RelationViolationError, match="marker facts"):
            source.install_at(destination.bus.log)
    assert hashes(destination.root) == before
