"""One-pass thread-list pending projection: no false zero or read acknowledgment."""

from __future__ import annotations

import json
import os
import threading

import pytest

from agent_comms.channel_targets import CommandTarget
from agent_comms.bus_activity_index import ActivityCheckpoint, BusActivityIndex
from agent_comms.bus_projection import AppendCheckpoint
from agent_comms.private_path import FileRevision
from agent_comms.read_basis import NamedViewer
from agent_comms.wire_log import CurrentSnapshotOpening
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.threads import Thread
from agent_comms.messages import Message


def _expected(wired, *, active_only: bool = False) -> dict[str, int]:
    selected = wired.registry.active_threads() if active_only else wired.registry.all_threads()
    return {name: wired.bus.pending_count(name) for name in selected}


def _listed(wired, *, active_only: bool = False) -> dict[str, int]:
    return {row.thread.name: row.pending.value for row in wired.views.list_threads(active_only=active_only)}


def test_mounted_viewer_snapshot_reopens_without_scanning_bus(wired, monkeypatch):
    wired.messaging.send("PR111", "#base", "channel activity").message.message_id
    wired.messaging.send("PR111", "fixer", "direct activity").message.message_id
    expected = wired.views.viewer_snapshot(str(wired.root))
    fresh = wire(wired.root)

    def forbidden_scan(*args, **kwargs):
        raise AssertionError("mounted snapshot reparsed historical bus rows")

    with monkeypatch.context() as idle:
        idle.setattr('agent_comms.wire_log.OpenedWireSnapshot.rows', forbidden_scan)
        idle.setattr('agent_comms.wire_log.OpenedWireSnapshot.public_records', forbidden_scan)
        assert fresh.views.viewer_snapshot(str(wired.root)) == expected

    wired.messaging.send("PR111", "#base", "new channel activity").message.message_id
    updated = fresh.views.viewer_snapshot(str(wired.root))
    assert updated.last_sent["PR111"] >= expected.last_sent["PR111"]
    assert updated == wire(wired.root).views.viewer_snapshot(str(wired.root))


def test_display_index_registers_once_and_only_view_snapshots_capture_indexes(wired):
    indexes = wired.bus.log._append_indexes
    baseline = len(indexes)
    first = wired.views.presentation.display_index("PR111")
    assert len(indexes) == baseline + 1
    assert wired.views.presentation.display_index("PR111") is first
    assert len(indexes) == baseline + 1
    with wired.bus.log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as source:
        assert source._append_checkpoints == {}
    with wired.views.presentation.snapshot(viewer=NamedViewer("PR111")) as (_basis, source):
        assert set(source._append_checkpoints) == set(indexes)


def test_historical_checkpoint_advances_disk_but_cannot_replace_a_farther_prefix(wired):
    wired.messaging.send("PR111", "#base", "first source row").message
    bus = wired.bus.log.path
    index = BusActivityIndex(bus)
    with bus.open("rb") as stream:
        first_source = FileRevision.from_stat(os.fstat(stream.fileno()))
        first = ActivityCheckpoint(
            first_source, first_source.size,
            AppendCheckpoint.fingerprint(stream, first_source.size), {}, {},
        )
    wired.messaging.send("PR111", "#base", "later source row").message
    index.write(first)
    saved = index.prepare()
    assert saved.offset == first.offset
    with bus.open("rb") as stream:
        latest_source = FileRevision.from_stat(os.fstat(stream.fileno()))
        latest = ActivityCheckpoint(
            latest_source, latest_source.size,
            AppendCheckpoint.fingerprint(stream, latest_source.size), {}, {},
        )
    index.write(latest)
    index.write(first)
    saved = index.prepare()
    assert saved.offset == latest.offset


def test_mounted_activity_rebuilds_damaged_checkpoint_and_refuses_bus_replacement(wired):
    wired.messaging.send("PR111", "#base", "first").message.message_id
    wired.messaging.send("fixer", "#base", "second").message.message_id
    assert set(wired.views.viewer_snapshot(str(wired.root)).last_sent) == {"PR111", "fixer"}

    checkpoint = wired.bus.activity_index.path
    checkpoint.write_text("{damaged")
    assert set(wire(wired.root).views.viewer_snapshot(str(wired.root)).last_sent) == {"PR111", "fixer"}

    # A valid JSON file can also lose projection values after local damage.
    # The bus is still authoritative when the checkpoint shape survives.
    cached = json.loads(checkpoint.read_text())
    cached["channels"]["#base"] = [0.0, 0.0]
    cached["sent"]["fixer"] = 0.0
    checkpoint.write_text(json.dumps(cached))
    reopened = wire(wired.root)
    assert reopened.views.viewer_snapshot(str(wired.root)).last_sent["fixer"] > 0.0
    with reopened.bus.opened_activity() as clocks:
        assert clocks.channels["#base"].last_message > 0.0

    bus_path = wired.bus.log.path
    replacement = bus_path.with_name("replacement.jsonl")
    replacement.write_bytes(bus_path.read_bytes().splitlines(keepends=True)[-1])
    os.chmod(replacement, 0o600)
    os.replace(replacement, bus_path)
    with pytest.raises(RelationViolationError, match="Private bus"):
        wire(wired.root).views.viewer_snapshot(str(wired.root))


def test_route_projection_refuses_atomic_bus_replacement(wired):
    wired.messaging.send("PR111", "fixer", "first").message.message_id
    wired.messaging.send("PR111", "fixer", "second").message.message_id
    assert _listed(wired)["fixer"] == 2
    bus_path = wired.bus.log.path
    rows = bus_path.read_bytes().splitlines(keepends=True)
    replacement = bus_path.with_name("replacement.jsonl")
    replacement.write_bytes(rows[1])
    os.chmod(replacement, 0o600)
    os.replace(replacement, bus_path)
    with pytest.raises(RelationViolationError, match="Private bus"):
        _listed(wire(wired.root))


def test_route_projection_refuses_rewrite_before_append(wired):
    wired.registry.declare(wired.registry.new_thread(name='third', tags=frozenset(), worktree='/tmp/third'))
    wired.messaging.send("PR111", "fixer", "first").message.message_id
    assert _listed(wired)["fixer"] == 1
    bus_path = wired.bus.log.path
    row = json.loads(bus_path.read_text())
    row["to"] = "third"
    bus_path.write_text(json.dumps(row) + "\n")
    tampered = bus_path.read_bytes()
    with pytest.raises(RelationViolationError, match="Private bus checkpoint prefix tail changed"):
        wired.messaging.send("PR111", "fixer", "second").message.message_id
    assert bus_path.read_bytes() == tampered


def test_rename_alias_and_real_thread_named_broadcast_match_existing_scope(wired):
    wired.registry.declare(wired.registry.new_thread(name='broadcast', tags=frozenset(), worktree='/tmp/broadcast'))
    wired.messaging.send("PR111", "broadcast", "direct message to the named thread").message.message_id
    wired.messaging.send("fixer", "broadcast", "second sender").message.message_id
    wired.messaging.send("PR111", "fixer", "old direct").message.message_id
    wired.registry.rename("PR111", "renamed")
    assert _listed(wired) == _expected(wired)
    references = tuple(message.reference for message in wired.bus.inbox("broadcast", CommandTarget.named("PR111")))
    wired.messaging.acknowledge("broadcast", references)
    assert _listed(wired) == _expected(wired)


@pytest.mark.parametrize(
    "change",
    [
        {"text": ""},
        {"type": "not-an-enum"},
        {"to": "bad/name"},
        {"mentions": [{"thread": "fixer", "start": 0, "end": 5}]},
        {"claim_transition": {"owner": "forged"}},
    ],
)
def test_invalid_wire_rows_fail_closed_like_existing_pending_count(wired, change):
    assert _listed(wired) == _expected(wired)
    row = {
        "seq": 1,
        "from": "PR111",
        "to": "fixer",
        "text": "ordinary body",
        "type": "info",
        "ts": 1.0,
    }
    row.update(change)
    with wired.bus.log.path.open("ab") as bus:
        bus.write((json.dumps(row) + "\n").encode())
    with pytest.raises((ValueError, KeyError, TypeError)):
        wired.bus.pending_count("fixer")
    with pytest.raises((ValueError, KeyError, TypeError)):
        wired.views.list_threads()


