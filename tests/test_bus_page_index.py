"""Indexed current history remains bounded and repairs disposable offset damage."""

from __future__ import annotations

import json
import sqlite3

from agent_comms.comms import Comms
from agent_comms.messages import Message, MessageType
from agent_comms.threads import Thread
from agent_comms.wire_metadata import ArchivedAccess


def retained_source(root, count):
    comms = Comms(root)
    for name in ("a", "b", "c"):
        comms.threads.register(Thread(name, frozenset(), str(root)))
    comms.messaging.initialize_private_initial_protocol()
    with comms.bus.log.path.open("w") as output:
        for seq in range(1, count + 1):
            row = Message("a", "b" if seq % 3 == 0 else "c", f"m{seq}", MessageType.INFO, seq=seq)
            output.write(json.dumps(row.to_wire()) + "\n")
    comms.bus.log.path.chmod(0o600)
    marker = comms.bus.log.read_metadata_unlocked(required=True)
    marker.last_seq = marker.admission_after_seq = count
    marker.access = ArchivedAccess()
    comms.bus.log.write_metadata_unlocked(marker)
    return comms.bus


def test_warm_incoming_cursor_reads_only_selected_wire_rows(tmp_path, monkeypatch):
    bus = retained_source(tmp_path, 3000)
    assert bus.incoming_page("b", after=2700, limit=10).newest_seq == 2730
    decoded = 0
    original = bus.log._public_page_record

    def count(record, size, metadata):
        nonlocal decoded
        decoded += 1
        return original(record, size, metadata)

    monkeypatch.setattr(bus.log, "_public_page_record", count)
    page = bus.incoming_page("b", after=2700, limit=10)
    assert [message.seq for message in page.messages] == list(range(2703, 2731, 3))
    assert page.has_older and page.has_newer
    assert decoded <= 12


def test_bad_cached_offset_uses_authoritative_current_source(tmp_path):
    bus = retained_source(tmp_path, 9)
    assert bus.incoming_page("b", after=6).newest_seq == 9
    with sqlite3.connect(tmp_path / "bus_page_index.sqlite3") as connection:
        connection.execute("UPDATE rows SET offset=0 WHERE seq=9")
    page = bus.incoming_page("b", after=6)
    assert [message.seq for message in page.messages] == [9]
