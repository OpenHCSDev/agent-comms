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
from agent_comms.errors import RelationViolationError
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.read_basis import DMDisplayScope
from agent_comms.threads import Thread
from agent_comms.store_files import _store_lock
from agent_comms.wire_log import OpenedWireSnapshot
from agent_comms.wire_metadata import ArchivedAccess, WritableAccess


def retained_source(root, count, *, access=ArchivedAccess()):
    comms = Comms(root)
    for name in ("a", "b", "c"):
        comms.registry.declare(Thread(name, frozenset(), str(root)))
    comms.messaging.initialize_private_initial_protocol()
    # Install the authored offline cut while actually holding source custody,
    # as in test_private_bus_checkpoint. Deleting its certificate before taking
    # the wire lock correctly makes normal readers refuse the fixture.
    with _store_lock(comms.root / 'wire'), comms.bus.log.locked():
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
        install_private_bus_checkpoint(comms.bus.log, _bus_locked=True)
    with BusPageIndex(comms.bus.log.path) as index:
        assert index.sync()
    marker = comms.bus.log.read_metadata_unlocked(required=True)
    marker.access = access
    comms.bus.log.write_metadata_unlocked(marker)
    return comms.bus


def test_absent_wire_has_empty_query_results_without_an_index(tmp_path):
    bus = Comms(tmp_path).bus
    assert bus.full_history_page().messages == ()
    scope = DMDisplayScope(frozenset({'a'}), frozenset({'b'}))
    assert not bus.conversation_older_unread(scope, before=1, seen=frozenset())
    assert not (tmp_path / 'bus_page_index.sqlite3').exists()


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


def test_older_unread_uses_only_pair_offsets_and_stops_at_first_match(tmp_path, monkeypatch):
    bus = retained_source(tmp_path, 30)
    scope = DMDisplayScope(frozenset({'b'}), frozenset({'a'}))
    decoded = []
    original = OpenedWireSnapshot.public_page_records

    def inspect(record, size, metadata):
        decoded.append(record['seq'])
        return original(record, size, metadata)

    def forbidden_scan(*args, **kwargs):
        raise AssertionError('Warm older-unread query must not scan the bus')
        yield

    monkeypatch.setattr(OpenedWireSnapshot, 'public_page_records', staticmethod(inspect))
    monkeypatch.setattr(OpenedWireSnapshot, 'public_records', forbidden_scan)
    monkeypatch.setattr(bus.log, '_record_snapshot', forbidden_scan)
    assert bus.conversation_older_unread(scope, before=25, seen=frozenset({24, 21}))
    assert decoded == [18]
    assert not bus.conversation_older_unread(scope, before=25, seen=frozenset(range(25)))
    assert decoded == [18]  # Seen rows need no body decoding.
    reverse = DMDisplayScope(scope.second_names, scope.first_names)
    assert not bus.conversation_older_unread(reverse, before=25, seen=frozenset())
    assert decoded == [18]  # Outbound is not unread for this viewer.
    with BusPageIndex(bus.log.path, readonly=True) as index:
        rows = tuple(scope.offsets(index, lower=None, upper=25, descending=True,
                                   before_offset=bus.log.path.stat().st_size))
    assert [row.seq for row in rows] == list(range(24, 0, -3))


def _forbid_sequential_read(*args, **kwargs):
    raise AssertionError('Page reads must prepare/query an index, not scan instead')
    yield


def test_bad_cached_offset_repairs_index_before_query(tmp_path, monkeypatch):
    bus = retained_source(tmp_path, 9, access=WritableAccess())
    assert bus.incoming_page("b", after=6).newest_seq == 9
    with sqlite3.connect(tmp_path / "bus_page_index.sqlite3") as connection:
        connection.execute(f"UPDATE {BusPageRow.declared_name} SET offset=0 WHERE seq=9")
    monkeypatch.setattr(OpenedWireSnapshot, 'public_records', _forbid_sequential_read)
    page = bus.incoming_page("b", after=6)
    assert [message.seq for message in page.messages] == [9]
    with BusPageIndex(bus.log.path, readonly=True) as index:
        row, = index.offsets(lower=8, upper=10, descending=False, targets=None)
        assert row.offset > 0


def test_missing_index_is_prepared_not_replaced_by_scan(tmp_path, monkeypatch):
    bus = retained_source(tmp_path, 9, access=WritableAccess())
    (tmp_path / 'bus_page_index.sqlite3').unlink()
    monkeypatch.setattr(OpenedWireSnapshot, 'public_records', _forbid_sequential_read)
    assert bus.incoming_page('b', after=6).newest_seq == 9
    with BusPageIndex(bus.log.path, readonly=True) as index:
        assert len(tuple(index.offsets(lower=None, upper=None, descending=False,
                                      targets=None))) == 9


def test_database_error_propagates_without_sequential_fallback(tmp_path, monkeypatch):
    bus = retained_source(tmp_path, 9, access=WritableAccess())
    failure = sqlite3.DatabaseError('real database failure')

    def broken(index, source):
        raise failure

    monkeypatch.setattr(BusPageIndex, 'prepare', broken)
    monkeypatch.setattr(OpenedWireSnapshot, 'public_records', _forbid_sequential_read)
    with pytest.raises(sqlite3.DatabaseError) as raised:
        bus.incoming_page('b', after=6)
    assert raised.value is failure


def test_archived_index_damage_is_visible_without_rewrite(tmp_path):
    bus = retained_source(tmp_path, 9)
    path = tmp_path / 'bus_page_index.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute(f"UPDATE {BusPageRow.declared_name} SET offset=0 WHERE seq=9")
    original = (path.read_bytes(), bus.log.path.read_bytes())
    with pytest.raises(RelationViolationError, match='offline preparation'):
        bus.incoming_page('b', after=6)
    assert (path.read_bytes(), bus.log.path.read_bytes()) == original


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
