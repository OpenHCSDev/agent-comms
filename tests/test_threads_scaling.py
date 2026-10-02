"""One-pass thread-list pending projection: no false zero or read acknowledgment."""

from __future__ import annotations

import json
import os
import threading

import pytest

from agent_comms.comms import wire
from agent_comms.threads import Thread
from agent_comms.messages import Message


def _expected(wired, *, active_only: bool = False) -> dict[str, int]:
    selected = wired.registry.active_threads() if active_only else wired.registry.all_threads()
    return {name: wired.bus.pending_count(name) for name in selected}


def _listed(wired, *, active_only: bool = False) -> dict[str, int]:
    return {row["name"]: row["pending"] for row in wired.views.list_threads(active_only=active_only)}


def test_one_pass_matches_dm_channel_broadcast_and_markers(wired, monkeypatch):
    wired.registry.declare(Thread(name="third", tags=frozenset({"base"}), worktree="/tmp/third"))
    wired.messaging.send("PR111", "fixer", "direct")
    wired.messaging.send("fixer", "PR111", "reverse direct")
    wired.messaging.send("PR111", "#all", "global")
    wired.messaging.send("fixer", "#base", "tagged")
    wired.messaging.send("third", "#all", "global from third")
    assert _listed(wired) == _expected(wired)
    assert wired.messaging.acknowledge("fixer", "PR111") == 1
    assert _listed(wired) == _expected(wired)
    wired.messaging.acknowledge("PR111")
    assert _listed(wired) == _expected(wired)
    wired.owners.stop("third")
    assert _listed(wired, active_only=True) == _expected(wired, active_only=True)

    # A fresh CLI process uses the durable projection without parsing history.
    fresh = wire(wired.root)
    calls = []
    original = Message.from_wire

    def measured(record):
        calls.append("scan")
        return original(record)

    monkeypatch.setattr(Message, "from_wire", measured)
    assert _listed(fresh) == _expected(wired)
    assert calls == []


def test_reopened_listing_does_not_parse_unchanged_bus_history(wired, monkeypatch):
    for index in range(100):
        wired.messaging.send("PR111", "fixer", f"message {index}")
    assert _listed(wired)["fixer"] == 100

    fresh = wire(wired.root)
    parsed = []
    original = Message.from_wire

    def measured(record):
        parsed.append(record["seq"])
        return original(record)

    monkeypatch.setattr(Message, "from_wire", measured)
    assert _listed(fresh)["fixer"] == 100
    assert parsed == []

    wired.messaging.send("PR111", "fixer", "new message")
    parsed.clear()  # Measure the read projection, separately from publisher validation.
    assert _listed(fresh)["fixer"] == 101
    assert parsed == [101]


def test_thread_listing_validates_registry_per_snapshot_not_per_row(wired, monkeypatch):
    for index in range(30):
        wired.registry.declare(Thread(f"peer-{index}", frozenset(), f"/peer-{index}"))
    checks = 0
    store_type = type(wired.registry.store)
    verify = store_type.private_guard_unlocked

    def counted_verify(store):
        nonlocal checks
        checks += 1
        return verify(store)

    monkeypatch.setattr(store_type, "private_guard_unlocked", counted_verify)
    rows = wired.views.list_threads()
    assert len(rows) == 32
    assert checks <= 3


def test_mounted_coordination_snapshot_reopens_without_scanning_bus(wired, monkeypatch):
    wired.messaging.send("PR111", "#base", "channel activity")
    wired.messaging.send("PR111", "fixer", "direct activity")
    expected = wired.views.coordination_snapshot()
    fresh = wire(wired.root)

    def forbidden_scan():
        raise AssertionError("mounted snapshot reparsed historical bus rows")

    monkeypatch.setattr(fresh.bus.log, '_iter_log_unlocked', forbidden_scan)
    assert fresh.views.coordination_snapshot() == expected

    wired.messaging.send("PR111", "#base", "new channel activity")
    updated = fresh.views.coordination_snapshot()
    assert updated.last_sent["PR111"] >= expected.last_sent["PR111"]
    assert updated == wire(wired.root).views.coordination_snapshot()


def test_activity_clocks_reuse_the_original_boundary_and_extend_on_append(wired, monkeypatch):
    first = wired.messaging.send_message("PR111", "#base", "channel clock")
    wired.messaging.send_message("fixer", "PR111", "direct clock")
    expected = wired.bus.channel_activity()
    sent = wired.bus.last_sent_timestamps()
    assert expected["#base"].last_message == first.timestamp
    fresh = wire(wired.root)
    parsed = []
    original = Message.from_wire

    def measured(record):
        parsed.append(record["seq"])
        return original(record)

    monkeypatch.setattr(Message, "from_wire", measured)
    assert fresh.bus.channel_activity() == expected
    assert fresh.bus.last_sent_timestamps() == sent
    assert parsed == []
    appended = wired.messaging.send_message("PR111", "#base", "appended clock")
    parsed.clear()  # Publisher validation belongs to a different lifetime.
    assert fresh.bus.channel_activity()["#base"].last_message == appended.timestamp
    assert fresh.bus.last_sent_timestamps()["PR111"] == appended.timestamp
    assert parsed == [appended.seq]


def test_mounted_activity_rebuilds_after_bus_replacement_or_damaged_checkpoint(wired):
    wired.messaging.send("PR111", "#base", "first")
    wired.messaging.send("fixer", "#base", "second")
    assert set(wired.views.coordination_snapshot().last_sent) == {"PR111", "fixer"}
    bus_path = wired.bus.log.path
    replacement = bus_path.with_name("replacement.jsonl")
    replacement.write_bytes(bus_path.read_bytes().splitlines(keepends=True)[-1])
    os.replace(replacement, bus_path)
    assert set(wire(wired.root).views.coordination_snapshot().last_sent) == {"fixer"}

    checkpoint = wired.root / "bus_activity_latest.json"
    checkpoint.write_text("{damaged")
    assert set(wire(wired.root).views.coordination_snapshot().last_sent) == {"fixer"}

    # A valid JSON file can also lose projection values after local damage.
    # The bus is still authoritative when the checkpoint shape survives.
    cached = json.loads(checkpoint.read_text())
    cached["channels"]["#base"] = [0.0, 0.0]
    cached["sent"]["fixer"] = 0.0
    checkpoint.write_text(json.dumps(cached))
    reopened = wire(wired.root)
    assert reopened.views.coordination_snapshot().last_sent["fixer"] > 0.0
    assert reopened.bus.channel_activity()["#base"].last_message > 0.0


def test_route_projection_rebuilds_after_atomic_bus_replacement(wired):
    wired.messaging.send("PR111", "fixer", "first")
    wired.messaging.send("PR111", "fixer", "second")
    assert _listed(wired)["fixer"] == 2
    bus_path = wired.bus.log.path
    rows = bus_path.read_bytes().splitlines(keepends=True)
    replacement = bus_path.with_name("replacement.jsonl")
    replacement.write_bytes(rows[1])
    os.replace(replacement, bus_path)
    assert _listed(wire(wired.root))["fixer"] == 1


def test_route_projection_detects_rewrite_before_append(wired):
    wired.registry.declare(Thread(name="third", tags=frozenset(), worktree="/tmp/third"))
    wired.messaging.send("PR111", "fixer", "first")
    assert _listed(wired)["fixer"] == 1
    bus_path = wired.bus.log.path
    row = json.loads(bus_path.read_text())
    row["to"] = "third"
    bus_path.write_text(json.dumps(row) + "\n")
    wired.messaging.send("PR111", "fixer", "second")
    counts = _listed(wire(wired.root))
    assert counts["fixer"] == 1
    assert counts["third"] == 1


def test_rename_alias_and_real_thread_named_broadcast_match_existing_scope(wired):
    wired.registry.declare(Thread(name="broadcast", tags=frozenset(), worktree="/tmp/broadcast"))
    wired.messaging.send("PR111", "broadcast", "direct message to the named thread")
    wired.messaging.send("fixer", "broadcast", "second sender")
    wired.messaging.send("PR111", "fixer", "old direct")
    wired.registry.rename("PR111", "renamed")
    assert _listed(wired) == _expected(wired)
    wired.messaging.acknowledge("broadcast", "PR111")
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


def test_projection_sync_holds_bus_lock_against_concurrent_append(wired, monkeypatch):
    wired.messaging.send("PR111", "fixer", "before")
    entered = threading.Event()
    release = threading.Event()
    original = Message.from_wire

    def gated(record):
        entered.set()
        assert release.wait(5)
        return original(record)

    monkeypatch.setattr(Message, "from_wire", gated)
    results: list[dict[str, int]] = []
    writer_done = threading.Event()
    reader = threading.Thread(target=lambda: results.append(_listed(wired)), daemon=True)
    writer = threading.Thread(
        target=lambda: (wired.messaging.send("PR111", "fixer", "after"), writer_done.set()),
        daemon=True,
    )
    try:
        reader.start()
        assert entered.wait(5)
        writer.start()
        assert not writer_done.wait(0.05)
    finally:
        release.set()
        reader.join(5)
        writer.join(5)
    assert not reader.is_alive() and not writer.is_alive()
    assert results[0]["fixer"] == 1
    assert _listed(wired)["fixer"] == 2
    assert [message.body for message in wired.bus.inbox("fixer")] == ["before", "after"]


def test_owner_tag_mutation_during_sync_cannot_ack_or_change_captured_audience(wired, monkeypatch):
    wired.messaging.send("PR111", "#auth", "eligible when listing began")
    entered = threading.Event()
    release = threading.Event()
    original = Message.from_wire

    def gated(record):
        entered.set()
        assert release.wait(5)
        return original(record)

    monkeypatch.setattr(Message, "from_wire", gated)
    results: list[dict[str, int]] = []
    reader = threading.Thread(target=lambda: results.append(_listed(wired)), daemon=True)
    writer = threading.Thread(
        target=lambda: wired.channels.update_tags("fixer", remove=frozenset({"auth"})), daemon=True
    )
    try:
        reader.start()
        assert entered.wait(5)
        writer.start()
    finally:
        release.set()
        reader.join(5)
        writer.join(5)
    assert not reader.is_alive() and not writer.is_alive()
    assert results[0]["fixer"] == 1  # audience captured before the mutation
    assert _listed(wired) == _expected(wired)  # next call sees changed tags
    assert not (wired.root / "read_markers.json").exists()  # listing never ACKs
