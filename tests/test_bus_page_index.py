"""Indexed current history remains bounded and repairs disposable offset damage."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_comms.bus_page_index import BusPageIndex, BusPageRow
from agent_comms.comms import Comms
from agent_comms.messages import InfoKind, Message
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.threads import Thread
from agent_comms.wire_log import OpenedWireSnapshot
from agent_comms.wire_metadata import ArchivedAccess


def retained_source(root, count):
    comms = Comms(root)
    for name in ("a", "b", "c"):
        comms.registry.declare(Thread(name, frozenset(), str(root)))
    comms.messaging.initialize_private_initial_protocol()
    with comms.bus.log.path.open("w") as output:
        for seq in range(1, count + 1):
            row = Message("a", "b" if seq % 3 == 0 else "c", f"m{seq}", InfoKind, seq=seq)
            output.write(json.dumps(row.to_wire()) + "\n")
    comms.bus.log.path.chmod(0o600)
    marker = comms.bus.log.read_metadata_unlocked(required=True)
    marker.last_seq = marker.admission_after_seq = count
    marker.checkpoint_version = marker.checkpoint_seal = None
    comms.bus.log.write_metadata_unlocked(marker)
    (root / "private_bus_checkpoint.sqlite3").unlink()
    install_private_bus_checkpoint(comms.bus.log)
    with BusPageIndex(comms.bus.log.path) as index:
        assert index.sync()
    marker = comms.bus.log.read_metadata_unlocked(required=True)
    marker.access = ArchivedAccess()
    comms.bus.log.write_metadata_unlocked(marker)
    return comms.bus


def test_warm_incoming_cursor_reads_only_selected_wire_rows(tmp_path, monkeypatch):
    bus = retained_source(tmp_path, 3000)
    assert bus.incoming_page("b", after=2700, limit=10).newest_seq == 2730
    decoded = 0
    original = OpenedWireSnapshot.public_page_records

    def count(record, size, metadata):
        nonlocal decoded
        decoded += 1
        return original(record, size, metadata)

    monkeypatch.setattr(OpenedWireSnapshot, "public_page_records", staticmethod(count))
    page = bus.incoming_page("b", after=2700, limit=10)
    assert [message.seq for message in page.messages] == list(range(2703, 2731, 3))
    assert page.has_older and page.has_newer
    assert decoded <= 12


def test_bad_cached_offset_uses_authoritative_current_source(tmp_path):
    bus = retained_source(tmp_path, 9)
    assert bus.incoming_page("b", after=6).newest_seq == 9
    with sqlite3.connect(tmp_path / "bus_page_index.sqlite3") as connection:
        connection.execute(f"UPDATE {BusPageRow.declared_name} SET offset=0 WHERE seq=9")
    page = bus.incoming_page("b", after=6)
    assert [message.seq for message in page.messages] == [9]


def test_parallel_preparation_commits_each_message_once(tmp_path):
    comms = Comms(tmp_path)
    for name in ("a", "b"):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path)))
    message = comms.messaging.send_message("a", "b", "once")
    prepared = threading.Barrier(2)

    class Together(BusPageIndex):
        batch_bytes = 1

        def _prepare(self, opened):
            result = super()._prepare(opened)
            assert not self.connection.in_transaction
            if result is not None and result[0] is None:
                prepared.wait(timeout=5)
            return result

    def index():
        with Together(comms.bus.log.path) as source:
            return source.sync()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [workers.submit(index) for _ in range(2)]
        assert all(result.result(timeout=10) for result in results)
    with BusPageIndex(comms.bus.log.path) as source:
        rows = tuple(source.offsets(lower=None, upper=None, descending=False, targets=None))
        assert [row.seq for row in rows] == [message.seq]
        with pytest.raises(sqlite3.IntegrityError), source.connection:
            rows[0].insert(source.connection)
    assert [row.seq for row in comms.bus.incoming_page("b", after=0).messages] == [message.seq]
