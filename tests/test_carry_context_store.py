"""Conversion uses only generated, stopped copies; never an installed bus."""

import json
import os
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest
from tools.cutover import carry_context_store as conversion

from agent_comms.checkpoint_seals import FinalSeal
from agent_comms.context_store import AbsentContext, ContextRequest
from agent_comms.envelope_claim_transitions import WakeClaim
from agent_comms.errors import HumanClaimBlockedError
from agent_comms.field_codec import FieldCodec
from agent_comms.private_bus_checkpoint import (
    _SEED,
    CheckedPrefix,
    CheckpointTable,
    ClaimCommits,
    DeliverySources,
    ResourceClaims,
    _chain,
    _connect,
    _path,
    _tail,
)
from agent_comms.private_path import FileRevision
from agent_comms.store_files import _store_lock
from agent_comms.typed_table import TypedTable
from agent_comms.wire_log import WireLog
from conftest import publish_initial
from test_context_manifest_deltas import context
from test_private_bus_checkpoint import _root


def old_copy(tmp_path):
    comms, root_id = _root(tmp_path)
    first = publish_initial(comms, "sender", "#team", "@Alice first")
    second = publish_initial(comms, "sender", "#team", "@Alice second")
    owned = tmp_path / "owned.txt"
    owned.write_text("owned\n")
    scope = WakeClaim(
        owner_claimed_generation=1,
        operation_id="f" * 32,
        turn_id="turn-one",
        wire_root_id=root_id,
        source_seq=first.seq,
        source_message_id=first.message_id,
        wake_assignment_id="cohort-v1:" + "d" * 64,
        wake_revision=1,
        recipient_lookup="e" * 32,
        execution_id="execution-one",
        participant_generation=1,
        attempt_ordinal=1,
    )
    with (
        comms.bus.opened_activity() as activity,
        _store_lock(comms._wire_lock_path),
        comms.bus.log.locked() as lock,
    ):
        snapshot = comms.registry.snapshot()
        alice = snapshot.require("Alice")
        claimed = comms.bus.publisher._publish_acquisition_unlocked(
            lock, snapshot, alice, str(owned), scope, activity=activity
        )
    original = comms.bus.log.path.read_bytes()
    write_metadata = WireLog.write_metadata_unlocked

    def crash_after_reservation(log, marker):
        write_metadata(log, marker)
        if marker.last_seq == claimed.seq + 1:
            raise OSError("generated unknown reservation")

    with (
        patch.object(WireLog, "write_metadata_unlocked", crash_after_reservation),
        pytest.raises(OSError, match="generated unknown reservation"),
    ):
        publish_initial(comms, "sender", "#team", "uncertain generated input")
    messages = original.splitlines(keepends=True)
    owner = comms.registry.require("Alice")
    expected = (context(owner, tmp_path, 2, 1), context(owner, tmp_path, 3, 2))
    marker = comms.bus.log.private_marker_unlocked()
    with closing(_connect(_path(comms.bus.log.path))) as db:
        conversion.ContextEntrySources.create(db)
        conversion.RecordedContextManifestSources.create(db)
        with comms.bus.log.path.open("w+b") as output:
            for number, manifest in enumerate(expected):
                if number == 1:
                    DeliverySources.update(
                        db, where="seq=?", parameters=(second.seq,), offset=output.tell()
                    )
                output.write(messages[number])
                recorded, entries = AbsentContext().capture(manifest)
                envelope = conversion.ContextEnvelope(
                    conversion.ContextEntryManifestObservation(entries)
                )
                raw = (json.dumps(FieldCodec.encode(envelope)) + "\n").encode()
                offset = output.tell()
                output.write(raw)
                for entry in entries:
                    reference = entry.reference
                    conversion.ContextEntrySources(
                        offset,
                        len(raw),
                        reference.identity.session_id,
                        reference.identity.session_file,
                        reference.entry_id,
                    ).insert_if_absent(db)
                envelope = conversion.ContextEnvelope(
                    conversion.RecordedContextManifestObservation(recorded)
                )
                raw = (json.dumps(FieldCodec.encode(envelope)) + "\n").encode()
                conversion.RecordedContextManifestSources(
                    output.tell(), len(raw), owner.incarnation
                ).insert(db)
                output.write(raw)
            output.write(messages[2])
            output.flush()
            os.fsync(output.fileno())
            size = output.tell()
            output.seek(0)
            digest = _SEED
            for raw in output:
                digest = _chain(digest, raw)
            prefix = CheckedPrefix.capture(
                root_id,
                FileRevision.from_stat(os.fstat(output.fileno())),
                claimed.seq,
                digest,
                _tail(output, size),
            )
        CheckedPrefix.delete(db, where="singleton=1")
        prefix.insert(db)
        db.commit()
    marker.seal_with(FinalSeal.capture(prefix.witness(), _path(comms.bus.log.path)))
    comms.bus.log.write_metadata_unlocked(marker)
    return comms, expected, original


def test_conversion_preserves_messages_claim_tables_and_context_values(tmp_path, monkeypatch):
    comms, expected, original = old_copy(tmp_path)
    tables = tuple(
        table
        for table in TypedTable.members_with(CheckpointTable)
        if table not in (CheckedPrefix, DeliverySources)
    )
    with closing(_connect(_path(comms.bus.log.path), readonly=True)) as db:
        before = {table: tuple(table.select(db)) for table in tables}
        assert ClaimCommits.count(db) == 1
        assert ResourceClaims.count(db) == 1
    readers = []
    connect, request = conversion._connect, conversion.ContextMove.request

    def track(path, **kwargs):
        db = connect(path, **kwargs)
        if kwargs.get("readonly"):
            readers.append(db)
        return db

    def check_closed(move, record, offset):
        # Reproduce the lifetime error without involving any live database.
        # Every original SQL reader must be closed before conversion starts.
        for db in readers:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                db.execute("SELECT 1")
        return request(move, record, offset)

    monkeypatch.setattr(conversion, "_connect", track)
    monkeypatch.setattr(conversion.ContextMove, "request", check_closed)
    with _store_lock(comms.root / "wire"):
        result = conversion.ContextManifests.carry(comms.root, apply=True)
    assert result.detail["requests"] == 2
    assert comms.bus.log.path.read_bytes() == original
    with closing(_connect(_path(comms.bus.log.path), readonly=True)) as db:
        witness = CheckedPrefix.read_witness(db)
        assert witness.offset == len(original)
        assert {table: tuple(table.select(db)) for table in tables} == before
    with (
        pytest.raises(HumanClaimBlockedError, match="UNKNOWN"),
        comms.bus.log.checked_read() as checked,
    ):
        assert checked.marker.last_seq == checked.witness.through_seq + 1
        checked.require_new_human_message("human", "new-message")
    page = comms.contexts.page("Alice", comms.registry)
    for actual, wanted in zip(page.resources, expected, strict=True):
        assert actual.value.segments[:2] == wanted.segments[:2]
        assert actual.value.segments[2].contributors == wanted.segments[2].contributors
    assert page.resources[1].recorded.segments[2].inherited_entries == 2
    assert [resource.recorded.recording_turn for resource in page.resources] == [1, 2]
    assert conversion.ContextManifests.carry(comms.root, apply=True).changed == 0
    assert not hasattr(conversion.ContextManifests, "copy")
    comms.contexts.record(context(comms.registry.require("Alice"), tmp_path, 4, 3))
    with comms.contexts.snapshot() as db:
        assert ContextRequest.count(db) == 3
    assert comms.contexts.page("Alice", comms.registry).resources[-1].recorded.recording_turn == 3


def test_failed_preparation_preserves_source_and_leaves_no_partial_store(tmp_path, monkeypatch):
    comms, _, _ = old_copy(tmp_path)
    protected = (comms.bus.log.path, _path(comms.bus.log.path), comms.root / "bus_meta.json")
    original = {path: path.read_bytes() for path in protected}
    request = conversion.ContextMove.request

    def interrupted(move, record, offset):
        request(move, record, offset)
        raise OSError("generated interruption before publication")

    with monkeypatch.context() as patcher:
        patcher.setattr(conversion.ContextMove, "request", interrupted)
        with (
            _store_lock(comms.root / "wire"),
            pytest.raises(OSError, match="before publication"),
        ):
            conversion.ContextManifests.carry(comms.root, apply=True)
    assert {path: path.read_bytes() for path in protected} == original
    assert not comms.contexts.path.exists()
    assert not list(comms.root.glob(".move-context-*"))
    with _store_lock(comms.root / "wire"):
        assert conversion.ContextManifests.carry(comms.root, apply=True).detail["requests"] == 2


def test_insufficient_space_refuses_before_staging_without_changing_root(tmp_path, monkeypatch):
    comms, _, _ = old_copy(tmp_path)
    filesystem = os.statvfs(comms.root)
    # Ask for more than the entire real filesystem, without mocking statvfs.
    reserve = filesystem.f_blocks * filesystem.f_frsize + 1
    monkeypatch.setattr(conversion.ContextManifests, "staging_reserve_bytes", reserve)
    required = (
        comms.bus.log.path.stat().st_size + _path(comms.bus.log.path).stat().st_size + reserve
    )
    with _store_lock(comms.root / "wire"):
        paths = set(comms.root.rglob("*"))
        original = {path: path.read_bytes() for path in paths if path.is_file()}
        with pytest.raises(OSError) as refusal:
            conversion.ContextManifests.carry(comms.root, apply=True)
        assert f"requires {required} bytes; available " in str(refusal.value)
        assert f"bytes on {comms.root}" in str(refusal.value)
        assert set(comms.root.rglob("*")) == paths
        assert {path: path.read_bytes() for path in original} == original
        assert not list(comms.root.glob(".move-context-*"))
