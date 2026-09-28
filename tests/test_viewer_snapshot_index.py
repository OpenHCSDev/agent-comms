"""A fresh sidebar view reuses validated bus history rather than decoding it."""

import json
import os
from contextlib import contextmanager
from unittest.mock import patch

from agent_comms.bus_display_index import BusDisplayIndex
from agent_comms.comms import wire
from agent_comms.messages import Message
from agent_comms.threads import Thread


def test_reopened_viewer_snapshot_decodes_only_appended_rows(tmp_path):
    comms = wire(tmp_path)
    comms.threads.register(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.threads.register(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    for number in range(100):
        comms.messaging.send("bob", "#team", f"message {number}")
    expected = comms.views.viewer_snapshot(str(tmp_path))
    assert expected.channel_unread["#team"] == 100

    fresh = wire(tmp_path)
    with patch.object(Message, "from_wire", wraps=Message.from_wire) as decode:
        assert fresh.views.viewer_snapshot(str(tmp_path)) == expected
        assert decode.call_count == 0

    comms.messaging.send("bob", "#team", "one new message")
    with patch.object(Message, "from_wire", wraps=Message.from_wire) as decode:
        latest = fresh.views.viewer_snapshot(str(tmp_path))
        assert latest.channel_unread["#team"] == 101
        # The activity and display projections each validate the new row.
        assert decode.call_count <= 2

    comms.channels.set_channel_any_mode("#team", True)
    assert fresh.views.viewer_snapshot(str(tmp_path)) == wire(tmp_path).views.viewer_snapshot(str(tmp_path))


def test_display_checkpoint_damage_and_bus_replacement_rebuild(tmp_path):
    comms = wire(tmp_path)
    comms.threads.register(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.threads.register(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    comms.messaging.send("bob", "#team", "first")
    comms.messaging.send("bob", "#team", "second")
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 2

    checkpoint = BusDisplayIndex(comms.bus.log.path, comms.messaging.user_identity(str(tmp_path)).name).path
    cached = json.loads(checkpoint.read_text())
    cached["counts"]["#team"] = 0
    checkpoint.write_text(json.dumps(cached))
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 2

    bus_path = comms.bus.log.path
    replacement = bus_path.with_name("replacement.jsonl")
    replacement.write_bytes(bus_path.read_bytes().splitlines(keepends=True)[-1])
    os.replace(replacement, bus_path)
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 1


def test_append_between_revision_and_opened_bus_boundary_uses_captured_records(
    tmp_path, monkeypatch
):
    comms = wire(tmp_path)
    comms.threads.register(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.threads.register(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    comms.messaging.send("bob", "#team", "first")
    bus_path = comms.bus.log.path
    first = json.loads(bus_path.read_text().splitlines()[0])
    raced_timestamp = first["ts"] + 10
    original = comms.bus.log._record_snapshot

    @contextmanager
    def append_before_open(*args, **kwargs):
        row = dict(first, seq=2, ts=raced_timestamp, text="raced append")
        with bus_path.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        with original(*args, **kwargs) as boundary:
            yield boundary

    monkeypatch.setattr(comms.bus.log, '_record_snapshot', append_before_open)
    snapshot = comms.views.viewer_snapshot(str(tmp_path))
    team = next(view for view in snapshot.channels if view.channel.name == "#team")
    assert snapshot.channel_unread["#team"] == 2
    assert team.last_activity == raced_timestamp


def test_reopened_human_pending_routes_keep_sparse_reads_aliases_and_fallback(tmp_path):
    import sqlite3
    from collections import Counter

    from agent_comms.bus_route_counts import BusRouteCounts

    comms = wire(tmp_path)
    comms.threads.register(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.threads.register(Thread("bob", frozenset(), str(tmp_path / "bob")))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    messages = [
        comms.messaging.send_message("bob", viewer if number % 2 else "#team", f"message {number}")
        for number in range(6)
    ]
    comms.messaging.send_user_message("bob", "own outgoing message", worktree=str(tmp_path))
    comms.views.viewer_snapshot(str(tmp_path))
    comms.bus.reads.mark_displayed(
        viewer,
        comms.bus.reads.capture(
            viewer, (messages[1], messages[4]), comms.registry.snapshot(), comms.bus.log.path
        ),
    )
    assert wire(tmp_path).bus.pending_counts(viewer) == {"bob": 2}
    comms.registry.rename("bob", "renamed-bob")
    delivery = comms.bus._delivery_scope(viewer)
    expected = Counter(
        delivery.conversation(message.sender, message.target) for message in comms.bus.inbox(viewer)
    )
    # The untagged human inbox excludes channel rows. Renaming invalidates
    # its old DM conversation evidence exactly as the authoritative inbox does.
    assert expected == {"renamed-bob": 3}
    with patch.object(Message, "from_wire", wraps=Message.from_wire) as decode:
        assert wire(tmp_path).bus.pending_counts(viewer) == expected
        assert decode.call_count == 0
    with patch.object(BusRouteCounts, "sync", side_effect=sqlite3.DatabaseError("unavailable")):
        assert wire(tmp_path).bus.pending_counts(viewer) == expected
