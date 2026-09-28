"""Saved-data conversion is explicit, lossless and uses the real A8 transaction."""

import json
import os
import stat
import subprocess
import sys
from dataclasses import replace

import pytest

from agent_comms import locked_store
from agent_comms.field_codec import FieldCodec
from agent_comms.relationship_migration import convert_relationships, migrate_relationships, preview
from agent_comms.relationships import CollaborationRevision, RelationshipDocument, RelationshipStore
from test_relationships import setup_wire


def old_document(comms):
    owner, peer = comms.registry.require("owner"), comms.registry.require("peer")
    first = CollaborationRevision(
        owner.name, peer.name, owner.created_at, peer.created_at, "Owner note", 10.0, 11.0
    )
    opposite = CollaborationRevision(
        peer.name, owner.name, peer.created_at, owner.created_at, "Peer note", 12.0, 13.0
    )
    payload = {
        "version": 1,
        "collaborations": [FieldCodec.encode(first), FieldCodec.encode(opposite)],
        "orders": [],
    }
    return payload, (first, opposite)


def test_explicit_migration_retires_duplicate_edges_and_keeps_every_original(tmp_path):
    comms = setup_wire(tmp_path)
    service = comms.relationships
    payload, originals = old_document(comms)
    service.store.path.write_text(json.dumps(payload))
    before = service.store.path.read_bytes()
    with pytest.raises(ValueError):
        service.collaborations("owner")
    assert service.store.path.read_bytes() == before  # No migration hidden in a normal read.
    converted = migrate_relationships(service.store.path, comms.registry.snapshot())
    (edge,) = converted.collaborations
    assert edge.note == "Owner note\nPeer note"
    assert edge.history == originals
    assert (edge.created_at, edge.updated_at) == (10.0, 13.0)
    assert converted == service.store.read()
    assert FieldCodec.decode(RelationshipDocument, FieldCodec.encode(converted)) == converted
    assert len(json.loads(service.store.path.read_text())["collaborations"]) == 1
    assert service.collaborations("peer")[0].peer == "owner"
    stat_before = service.store.path.stat()
    assert migrate_relationships(service.store.path, comms.registry.snapshot()) == converted
    assert service.store.path.stat() == stat_before
    service.edit("owner", "add", "child", "Independent work")
    kept = service.collaborations("owner")[0]
    assert kept.history == originals
    updated = service.edit("owner", "update", "peer", "Current plan")
    assert updated.note == "Current plan"
    assert updated.history[:-1] == originals
    assert updated.history[-1].note == "Owner note\nPeer note"


def test_conversion_resolves_real_aliases_without_rebinding_or_erasing_provenance(tmp_path):
    comms = setup_wire(tmp_path)
    payload, originals = old_document(comms)
    comms.registry.rename("peer", "reviewer")
    # An existing alias is actual identity data. Opposite rows may already use the new name.
    payload["collaborations"][1]["owner"] = "reviewer"
    renamed_original = replace(originals[1], owner="reviewer")
    # An old incarnation with the same spelling remains separate historical work.
    stale = replace(
        originals[0], peer_created=originals[0].peer_created - 1, note="Old incarnation"
    )
    payload["collaborations"].append(FieldCodec.encode(stale))
    converted = convert_relationships(payload, comms.registry.snapshot())
    assert len(converted.collaborations) == 2
    live, old = converted.collaborations
    assert live.history == (originals[0], renamed_original)
    assert {live.owner, live.peer} == {"owner", "reviewer"}
    assert old.peer == "peer" and old.note == stale.note and old.peer_created == stale.peer_created
    assert comms.registry.snapshot().aliases["peer"] == "reviewer"


@pytest.mark.parametrize("stage", ["file_sync", "replace", "directory_sync"])
def test_failed_migration_retains_original_document_and_mode(tmp_path, monkeypatch, stage):
    comms = setup_wire(tmp_path)
    payload, _ = old_document(comms)
    path = comms.relationships.store.path
    path.write_text(json.dumps(payload))
    path.chmod(0o640)
    before = path.read_bytes()
    real_sync, real_replace = os.fsync, locked_store._replace_snapshot
    failed = False

    def sync(fd):
        nonlocal failed
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if not failed and (
            (stage == "directory_sync" and directory) or (stage == "file_sync" and not directory)
        ):
            failed = True
            raise OSError("migration sync failed")
        return real_sync(fd)

    def publish(source, target):
        if stage == "replace":
            raise OSError("migration publication failed")
        return real_replace(source, target)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(locked_store, "_replace_snapshot", publish)
    with pytest.raises(OSError, match="migration"):
        migrate_relationships(path, comms.registry.snapshot())
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert not list(path.parent.glob(".*.previous"))
    assert not list(path.parent.glob(".*.tmp"))


@pytest.mark.parametrize("where", ["document", "record"])
def test_unrecognized_actual_data_is_rejected_without_dropping_it(tmp_path, where):
    comms = setup_wire(tmp_path)
    payload, _ = old_document(comms)
    container = payload if where == "document" else payload["collaborations"][0]
    container["unrecognized_data"] = "must not disappear"
    path = comms.relationships.store.path
    path.write_text(json.dumps(payload))
    original = path.read_bytes()
    with pytest.raises(ValueError):
        migrate_relationships(path, comms.registry.snapshot())
    assert path.read_bytes() == original


def test_preview_and_actual_cli_migration_are_separate(tmp_path):
    comms = setup_wire(tmp_path)
    payload, _ = old_document(comms)
    path = comms.relationships.store.path
    path.write_text(json.dumps(payload))
    original = path.read_bytes()
    assert preview(comms.root).collaborations[0].history
    assert path.read_bytes() == original
    command = [
        sys.executable,
        "-m",
        "agent_comms.relationship_migration",
        "--root",
        str(comms.root),
    ]
    before = {file.name: file.stat().st_mtime_ns for file in comms.root.iterdir()}
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    assert json.loads(result.stdout)["applied"] is False
    assert before == {file.name: file.stat().st_mtime_ns for file in comms.root.iterdir()}
    result = subprocess.run([*command, "--apply"], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout)["historical_records"] == 2
    assert RelationshipStore(path).read().version == 2


def test_missing_optional_document_is_not_created_by_migration(tmp_path):
    comms = setup_wire(tmp_path)
    path = comms.relationships.store.path
    assert migrate_relationships(path, comms.registry.snapshot()) == RelationshipDocument()
    assert not path.exists()


def test_unknown_peer_keeps_registry_error_and_does_not_create_document(tmp_path):
    from agent_comms.declarations import UnregisteredThreadError

    comms = setup_wire(tmp_path)
    with pytest.raises(UnregisteredThreadError):
        comms.relationships.edit("owner", "add", "missing", "Uncommitted note")
    assert not comms.relationships.store.path.exists()
