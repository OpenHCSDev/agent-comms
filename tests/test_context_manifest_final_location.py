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
from agent_comms.child_process import Platform
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.private_bus_checkpoint import _directory_sync, install_private_bus_checkpoint
from agent_comms.store_files import _store_lock_file
from agent_comms.wire_log import WireLog
from test_context_manifest_carry import original_root, hashes
from tools.cutover.carry_context_manifest_rows import ManifestRowCarry


def test_final_location_recertifies_carry_and_reads_retained_history(tmp_path):
    original, root_id, manifest, messages = original_root(tmp_path)
    before = hashes(original.root)
    final_root = tmp_path / "final-location"
    shutil.copytree(original.root, final_root)
    final_log = WireLog(final_root / "bus.jsonl")
    # Copying files preserves bytes, not their inode-bound certificates.
    with pytest.raises(RelationViolationError):
        with final_log.certified_read():
            pytest.fail("Copied original certificate must not authorize the new inode")
    candidate = tmp_path / "candidate"
    carried = ManifestRowCarry(original.root, candidate, root_id).run()
    original_marker = final_log.read_metadata_unlocked()
    unsealed = replace(original_marker, checkpoint_version=None, checkpoint_seal=None)
    platform = Platform.current()
    # This isolated copy has no writers. Borrow the physical canonical lock
    # directly: its copied certificate cannot admit a normal writer/read lock.
    # Never execute this fixture recipe against a public or live root.
    with _store_lock_file(final_log.path) as lock_file:
        platform.acquire_store_lock(lock_file.fileno(), shared=False, blocking=True)
        try:
            os.replace(candidate / "bus.jsonl", final_log.path)
            _directory_sync(final_root)
            final_log.write_metadata_unlocked(unsealed)
            (final_root / "private_bus_checkpoint.sqlite3").unlink()
            witness = install_private_bus_checkpoint(final_log, _bus_locked=True)
        finally:
            platform.release_store_lock(lock_file.fileno())
    assert witness.revision == file_revision(final_log.path.stat())
    assert witness.digest == carried["candidate_certificate"]["digest"]
    assert witness.revision != tuple(carried["candidate_certificate"]["revision"])
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
