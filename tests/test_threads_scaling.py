"""One-pass thread-list pending projection: no false zero or read acknowledgment."""

from __future__ import annotations

import json
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

    # In a fresh CLI process there is no per-viewer cache. Still only one
    # wire parser pass, including when stopped threads remain in the registry.
    fresh = wire(wired.root)
    calls = []
    original = fresh.bus._iter_pending_routes_unlocked

    def measured():
        calls.append("scan")
        yield from original()

    monkeypatch.setattr(fresh.bus, "_iter_pending_routes_unlocked", measured)
    assert _listed(fresh) == _expected(wired)
    assert calls == ["scan"]


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


def test_single_scan_holds_bus_lock_against_concurrent_append(wired, monkeypatch):
    wired.send("PR111", "fixer", "before")
    entered = threading.Event()
    release = threading.Event()
    original = wired.bus._iter_pending_routes_unlocked

    def gated():
        for message in original():
            entered.set()
            assert release.wait(5)
            yield message

    monkeypatch.setattr(wired.bus, "_iter_pending_routes_unlocked", gated)
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


def test_owner_tag_mutation_during_scan_cannot_ack_or_change_captured_audience(wired, monkeypatch):
    wired.send("PR111", "#auth", "eligible when listing began")
    entered = threading.Event()
    release = threading.Event()
    original = wired.bus._iter_pending_routes_unlocked

    def gated():
        for route in original():
            entered.set()
            assert release.wait(5)
            yield route

    monkeypatch.setattr(wired.bus, "_iter_pending_routes_unlocked", gated)
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
