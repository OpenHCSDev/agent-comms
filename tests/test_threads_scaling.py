"""One-pass thread-list pending projection: no false zero or read acknowledgment."""

from __future__ import annotations

import json
import os
import threading

import pytest

from agent_comms import Thread, wire


def _expected(wired, *, active_only: bool = False) -> dict[str, int]:
    selected = wired.registry.active_threads() if active_only else wired.registry.all_threads()
    return {name: wired.pending_count(name) for name in selected}


def _listed(wired, *, active_only: bool = False) -> dict[str, int]:
    return {row["name"]: row["pending"] for row in wired.list_threads(active_only=active_only)}


def test_one_pass_matches_dm_channel_broadcast_and_markers(wired, monkeypatch):
    wired.register(Thread(name="third", tags=frozenset({"base"}), worktree="/tmp/third"))
    wired.send("PR111", "fixer", "direct")
    wired.send("fixer", "PR111", "reverse direct")
    wired.send("PR111", "#all", "global")
    wired.send("fixer", "#base", "tagged")
    wired.send("third", "broadcast", "legacy broadcast")
    assert _listed(wired) == _expected(wired)
    assert wired.acknowledge("fixer", "PR111") == 1
    assert _listed(wired) == _expected(wired)
    wired.acknowledge("PR111")
    assert _listed(wired) == _expected(wired)
    wired.stop("third")
    assert _listed(wired, active_only=True) == _expected(wired, active_only=True)

    # A fresh CLI process uses the durable projection without parsing history.
    fresh = wire(wired.root)
    calls = []
    original = fresh.bus._pending_route_fields

    def measured(record):
        calls.append("scan")
        return original(record)

    monkeypatch.setattr(fresh.bus, "_pending_route_fields", measured)
    assert _listed(fresh) == _expected(wired)
    assert calls == []


def test_reopened_listing_does_not_parse_unchanged_bus_history(wired, monkeypatch):
    for index in range(100):
        wired.send("PR111", "fixer", f"message {index}")
    assert _listed(wired)["fixer"] == 100

    fresh = wire(wired.root)
    parsed = []
    original = fresh.bus._pending_route_fields

    def measured(record):
        parsed.append(record["seq"])
        return original(record)

    monkeypatch.setattr(fresh.bus, "_pending_route_fields", measured)
    assert _listed(fresh)["fixer"] == 100
    assert parsed == []

    wired.send("PR111", "fixer", "new message")
    assert _listed(fresh)["fixer"] == 101
    assert parsed == [101]


def test_thread_listing_validates_registry_per_snapshot_not_per_row(wired, monkeypatch):
    for index in range(30):
        wired.register(Thread(f"peer-{index}", frozenset(), f"/peer-{index}"))
    checks = 0
    verify = wired.registry._private_guard_unlocked

    def counted_verify():
        nonlocal checks
        checks += 1
        return verify()

    monkeypatch.setattr(wired.registry, "_private_guard_unlocked", counted_verify)
    rows = wired.list_threads()
    assert len(rows) == 32
    assert checks <= 3


def test_mounted_coordination_snapshot_reopens_without_scanning_bus(wired, monkeypatch):
    wired.send("PR111", "#base", "channel activity")
    wired.send("PR111", "fixer", "direct activity")
    expected = wired.coordination_snapshot()
    fresh = wire(wired.root)

    def forbidden_scan():
        raise AssertionError("mounted snapshot reparsed historical bus rows")

    monkeypatch.setattr(fresh.bus, "_iter_log_unlocked", forbidden_scan)
    assert fresh.coordination_snapshot() == expected

    wired.send("PR111", "#base", "new channel activity")
    updated = fresh.coordination_snapshot()
    assert updated.last_sent["PR111"] >= expected.last_sent["PR111"]
    assert updated == wire(wired.root).coordination_snapshot()


def test_mounted_activity_rebuilds_after_bus_replacement_or_damaged_checkpoint(wired):
    wired.send("PR111", "#base", "first")
    wired.send("fixer", "#base", "second")
    assert set(wired.coordination_snapshot().last_sent) == {"PR111", "fixer"}
    bus_path = wired.bus._path
    replacement = bus_path.with_name("replacement.jsonl")
    replacement.write_bytes(bus_path.read_bytes().splitlines(keepends=True)[-1])
    os.replace(replacement, bus_path)
    assert set(wire(wired.root).coordination_snapshot().last_sent) == {"fixer"}

    checkpoint = wired.root / "bus_activity_latest.json"
    checkpoint.write_text("{damaged")
    assert set(wire(wired.root).coordination_snapshot().last_sent) == {"fixer"}

    # A valid JSON file can also lose projection values after local damage.
    # The bus is still authoritative when the checkpoint shape survives.
    cached = json.loads(checkpoint.read_text())
    cached["channels"]["#base"] = [0.0, 0.0]
    cached["sent"]["fixer"] = 0.0
    checkpoint.write_text(json.dumps(cached))
    reopened = wire(wired.root)
    assert reopened.coordination_snapshot().last_sent["fixer"] > 0.0
    assert reopened.bus.channel_activity()["#base"].last_message > 0.0


def test_route_projection_rebuilds_after_atomic_bus_replacement(wired):
    wired.send("PR111", "fixer", "first")
    wired.send("PR111", "fixer", "second")
    assert _listed(wired)["fixer"] == 2
    bus_path = wired.bus._path
    rows = bus_path.read_bytes().splitlines(keepends=True)
    replacement = bus_path.with_name("replacement.jsonl")
    replacement.write_bytes(rows[1])
    os.replace(replacement, bus_path)
    assert _listed(wire(wired.root))["fixer"] == 1


def test_route_projection_detects_rewrite_before_append(wired):
    wired.register(Thread(name="third", tags=frozenset(), worktree="/tmp/third"))
    wired.send("PR111", "fixer", "first")
    assert _listed(wired)["fixer"] == 1
    bus_path = wired.bus._path
    row = json.loads(bus_path.read_text())
    row["to"] = "third"
    bus_path.write_text(json.dumps(row) + "\n")
    wired.send("PR111", "fixer", "second")
    counts = _listed(wire(wired.root))
    assert counts["fixer"] == 1
    assert counts["third"] == 1


def test_rename_alias_and_real_thread_named_broadcast_match_existing_scope(wired):
    wired.register(Thread(name="broadcast", tags=frozenset(), worktree="/tmp/broadcast"))
    wired.send("PR111", "broadcast", "channel alias also names a real thread")
    wired.send("fixer", "broadcast", "second sender")
    wired.send("PR111", "fixer", "old direct")
    wired.registry.rename("PR111", "renamed")
    wired.bus.rename_thread("PR111", "renamed")
    assert _listed(wired) == _expected(wired)
    wired.acknowledge("broadcast", "PR111")
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
    with wired.bus._path.open("ab") as bus:
        bus.write((json.dumps(row) + "\n").encode())
    with pytest.raises((ValueError, KeyError, TypeError)):
        wired.pending_count("fixer")
    with pytest.raises((ValueError, KeyError, TypeError)):
        wired.list_threads()


def test_projection_sync_holds_bus_lock_against_concurrent_append(wired, monkeypatch):
    wired.send("PR111", "fixer", "before")
    entered = threading.Event()
    release = threading.Event()
    original = wired.bus._pending_route_fields

    def gated(record):
        entered.set()
        assert release.wait(5)
        return original(record)

    monkeypatch.setattr(wired.bus, "_pending_route_fields", gated)
    results: list[dict[str, int]] = []
    writer_done = threading.Event()
    reader = threading.Thread(target=lambda: results.append(_listed(wired)), daemon=True)
    writer = threading.Thread(
        target=lambda: (wired.send("PR111", "fixer", "after"), writer_done.set()),
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
    assert [message.body for message in wired.inbox("fixer")] == ["before", "after"]


def test_owner_tag_mutation_during_sync_cannot_ack_or_change_captured_audience(wired, monkeypatch):
    wired.send("PR111", "#auth", "eligible when listing began")
    entered = threading.Event()
    release = threading.Event()
    original = wired.bus._pending_route_fields

    def gated(record):
        entered.set()
        assert release.wait(5)
        return original(record)

    monkeypatch.setattr(wired.bus, "_pending_route_fields", gated)
    results: list[dict[str, int]] = []
    reader = threading.Thread(target=lambda: results.append(_listed(wired)), daemon=True)
    writer = threading.Thread(
        target=lambda: wired.update_tags("fixer", remove=frozenset({"auth"})), daemon=True
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
