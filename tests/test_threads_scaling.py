"""One-pass thread-list pending projection: no false zero or read acknowledgment."""

from __future__ import annotations

import json
import os
import threading

import pytest

from agent_comms.bus_activity_index import ActivityCheckpoint, BusActivityIndex
from agent_comms.bus_projection import AppendCheckpoint
from agent_comms.private_path import FileRevision
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
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
    references = tuple(message.reference for message in wired.bus.inbox("fixer", "PR111"))
    assert wired.messaging.acknowledge("fixer", references) == 1
    assert _listed(wired) == _expected(wired)
    messages = wired.bus.inbox("PR111")
    wired.messaging.acknowledge("PR111", tuple(message.reference for message in messages))
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
    wired.messaging.initialize_private_initial_protocol()
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


@pytest.mark.parametrize("same_owner", [False, True])
def test_open_view_cut_keeps_its_append_checkpoints(wired, monkeypatch, same_owner):
    first = wired.messaging.send_message("PR111", "#base", "first view activity")
    viewer = wired.messaging.user_identity("/isolated-checkpoint-viewer").name
    # Prepare both actual index owners before taking a cold observer's cut.
    wired.views.viewer_snapshot("/isolated-checkpoint-viewer")
    captured = wired if same_owner else wire(wired.root)
    with captured.views.presentation.snapshot(viewer=viewer) as (basis, source):
        appended = wired.messaging.send_message("fixer", "#base", "later activity")
        # B advances both index owners; A keeps the prefix read with its source.
        wired.views.viewer_snapshot("/isolated-checkpoint-viewer")
        parsed = []
        original = Message.from_wire

        def measured(row):
            parsed.append(row["seq"])
            return original(row)

        monkeypatch.setattr(Message, "from_wire", measured)
        activity, unread = captured.views.presentation.display_view_metrics(source, basis)
        clocks = captured.bus.last_sent_timestamps(source=source)
        assert clocks["PR111"] == first.timestamp
        assert clocks.get("fixer", 0) < appended.timestamp
        assert activity["#base"].last_message == first.timestamp
        assert parsed == []
    # The historical reader did not overwrite the newer published checkpoint.
    parsed.clear()
    fresh = wire(wired.root)
    assert fresh.bus.last_sent_timestamps()["fixer"] == appended.timestamp
    assert parsed == []


def test_computed_checkpoint_stays_with_the_same_opened_cut(wired, monkeypatch):
    first = wired.messaging.send_message("PR111", "#base", "first exact-cut row")
    viewer = wired.messaging.user_identity("/same-cut-viewer").name
    with wired.views.presentation.snapshot(viewer=viewer) as (basis, source):
        parsed = []
        original = Message.from_wire

        def measured(row):
            parsed.append(row["seq"])
            return original(row)

        monkeypatch.setattr(Message, "from_wire", measured)
        first_metrics = wired.views.presentation.display_view_metrics(source, basis)
        first_clocks = wired.bus.last_sent_timestamps(source=source)
        assert first.seq in parsed
        parsed.clear()
        assert wired.views.presentation.display_view_metrics(source, basis) == first_metrics
        assert wired.bus.last_sent_timestamps(source=source) == first_clocks
        assert parsed == []


def test_display_index_registers_once_and_only_view_snapshots_capture_indexes(wired):
    indexes = wired.bus.log._append_indexes
    baseline = len(indexes)
    first = wired.views.presentation.display_index("PR111")
    assert len(indexes) == baseline + 1
    assert wired.views.presentation.display_index("PR111") is first
    assert len(indexes) == baseline + 1
    with wired.bus.log._opened_wire_snapshot(need_sequence=False) as source:
        assert source._append_checkpoints == {}
    with wired.views.presentation.snapshot(viewer="PR111") as (_basis, source):
        assert set(source._append_checkpoints) == set(indexes)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file-lock observation")
def test_checkpoint_reads_and_digests_run_outside_wire_and_bus_locks(wired, monkeypatch):
    import fcntl
    from agent_comms.bus_projection import BusAppendIndex

    wired.messaging.send_message("PR111", "#base", "checkpoint lock scope")
    reads, checks = [], []
    read = BusAppendIndex._read
    accepts = AppendCheckpoint.accepts

    def unlocked():
        for name in (".wire.lock", ".bus.jsonl.lock"):
            with (wired.root / name).open("rb") as descriptor:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def observed_read(index):
        unlocked()
        reads.append(index.path)
        return read(index)

    def observed_accepts(record, revision, stream):
        unlocked()
        checks.append(record.offset)
        return accepts(record, revision, stream)

    monkeypatch.setattr(BusAppendIndex, "_read", observed_read)
    monkeypatch.setattr(AppendCheckpoint, "accepts", observed_accepts)
    wired.views.viewer_snapshot("/lock-scope-viewer")
    assert reads and checks
    # Later cuts start from the checkpoint each index holds; only a write
    # still reads the saved file, to keep another process's farther prefix.
    prepared = []
    prepare = BusAppendIndex.prepare
    monkeypatch.setattr(BusAppendIndex, "_read", read)
    monkeypatch.setattr(
        BusAppendIndex, "prepare", lambda index: prepared.append(index._held) or prepare(index)
    )
    wired.messaging.send_message("PR111", "#base", "later row")
    wired.views.viewer_snapshot("/lock-scope-viewer")
    assert prepared and None not in prepared


def test_historical_checkpoint_advances_disk_but_cannot_replace_a_farther_prefix(wired):
    wired.messaging.send_message("PR111", "#base", "first source row")
    bus = wired.bus.log.path
    index = BusActivityIndex(bus)
    with bus.open("rb") as stream:
        first_source = FileRevision.from_stat(os.fstat(stream.fileno()))
        first = ActivityCheckpoint(
            first_source, first_source.size,
            AppendCheckpoint.fingerprint(stream, first_source.size), {}, {},
        )
    wired.messaging.send_message("PR111", "#base", "later source row")
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
    wired.messaging.send("PR111", "#base", "first")
    wired.messaging.send("fixer", "#base", "second")
    assert set(wired.views.coordination_snapshot().last_sent) == {"PR111", "fixer"}

    checkpoint = wired.root / "bus_activity_latest.json"
    checkpoint.write_text("{damaged")
    assert set(wire(wired.root).views.coordination_snapshot().last_sent) == {"PR111", "fixer"}

    # A valid JSON file can also lose projection values after local damage.
    # The bus is still authoritative when the checkpoint shape survives.
    cached = json.loads(checkpoint.read_text())
    cached["channels"]["#base"] = [0.0, 0.0]
    cached["sent"]["fixer"] = 0.0
    checkpoint.write_text(json.dumps(cached))
    reopened = wire(wired.root)
    assert reopened.views.coordination_snapshot().last_sent["fixer"] > 0.0
    assert reopened.bus.channel_activity()["#base"].last_message > 0.0

    bus_path = wired.bus.log.path
    replacement = bus_path.with_name("replacement.jsonl")
    replacement.write_bytes(bus_path.read_bytes().splitlines(keepends=True)[-1])
    os.chmod(replacement, 0o600)
    os.replace(replacement, bus_path)
    with pytest.raises(RelationViolationError, match="Private bus"):
        wire(wired.root).views.coordination_snapshot()


def test_route_projection_refuses_atomic_bus_replacement(wired):
    wired.messaging.send("PR111", "fixer", "first")
    wired.messaging.send("PR111", "fixer", "second")
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
    wired.registry.declare(Thread(name="third", tags=frozenset(), worktree="/tmp/third"))
    wired.messaging.send("PR111", "fixer", "first")
    assert _listed(wired)["fixer"] == 1
    bus_path = wired.bus.log.path
    row = json.loads(bus_path.read_text())
    row["to"] = "third"
    bus_path.write_text(json.dumps(row) + "\n")
    tampered = bus_path.read_bytes()
    with pytest.raises(RelationViolationError, match="Private bus checkpoint prefix tail changed"):
        wired.messaging.send("PR111", "fixer", "second")
    assert bus_path.read_bytes() == tampered


def test_rename_alias_and_real_thread_named_broadcast_match_existing_scope(wired):
    wired.registry.declare(Thread(name="broadcast", tags=frozenset(), worktree="/tmp/broadcast"))
    wired.messaging.send("PR111", "broadcast", "direct message to the named thread")
    wired.messaging.send("fixer", "broadcast", "second sender")
    wired.messaging.send("PR111", "fixer", "old direct")
    wired.registry.rename("PR111", "renamed")
    assert _listed(wired) == _expected(wired)
    references = tuple(message.reference for message in wired.bus.inbox("broadcast", "PR111"))
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
