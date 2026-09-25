"""A fresh sidebar view reuses validated bus history rather than decoding it."""

import json
import os
from contextlib import contextmanager
from unittest.mock import patch

from agent_comms import Thread, wire
from agent_comms.bus_display_index import BusDisplayIndex
from agent_comms.declarations import Message


def test_reopened_viewer_snapshot_decodes_only_appended_rows(tmp_path):
    comms = wire(tmp_path)
    comms.register(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.register(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    for number in range(100):
        comms.send("bob", "#team", f"message {number}")
    expected = comms.viewer_snapshot(str(tmp_path))
    assert expected.channel_unread["#team"] == 100

    fresh = wire(tmp_path)
    with patch.object(Message, "from_wire", wraps=Message.from_wire) as decode:
        assert fresh.viewer_snapshot(str(tmp_path)) == expected
        assert decode.call_count == 0

    comms.send("bob", "#team", "one new message")
    with patch.object(Message, "from_wire", wraps=Message.from_wire) as decode:
        latest = fresh.viewer_snapshot(str(tmp_path))
        assert latest.channel_unread["#team"] == 101
        # The activity and display projections each validate the new row.
        assert decode.call_count <= 2

    comms.set_channel_any_mode("#team", True)
    assert fresh.viewer_snapshot(str(tmp_path)) == wire(tmp_path).viewer_snapshot(str(tmp_path))


def test_display_checkpoint_damage_and_bus_replacement_rebuild(tmp_path):
    comms = wire(tmp_path)
    comms.register(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.register(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    comms.send("bob", "#team", "first")
    comms.send("bob", "#team", "second")
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 2

    checkpoint = BusDisplayIndex(comms.bus._path, comms.user_identity(str(tmp_path)).name).path
    cached = json.loads(checkpoint.read_text())
    cached["counts"]["#team"] = 0
    checkpoint.write_text(json.dumps(cached))
    assert wire(tmp_path).viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 2

    bus_path = comms.bus._path
    replacement = bus_path.with_name("replacement.jsonl")
    replacement.write_bytes(bus_path.read_bytes().splitlines(keepends=True)[-1])
    os.replace(replacement, bus_path)
    assert wire(tmp_path).viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 1


def test_append_between_revision_and_opened_bus_boundary_uses_captured_records(
    tmp_path, monkeypatch
):
    comms = wire(tmp_path)
    comms.register(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.register(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    comms.send("bob", "#team", "first")
    bus_path = comms.bus._path
    first = json.loads(bus_path.read_text().splitlines()[0])
    raced_timestamp = first["ts"] + 10
    original = comms.bus._record_snapshot

    @contextmanager
    def append_before_open(*args, **kwargs):
        row = dict(first, seq=2, ts=raced_timestamp, text="raced append")
        with bus_path.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        with original(*args, **kwargs) as boundary:
            yield boundary

    monkeypatch.setattr(comms.bus, "_record_snapshot", append_before_open)
    snapshot = comms.viewer_snapshot(str(tmp_path))
    team = next(view for view in snapshot.channels if view.channel.name == "#team")
    assert snapshot.channel_unread["#team"] == 2
    assert team.last_activity == raced_timestamp
