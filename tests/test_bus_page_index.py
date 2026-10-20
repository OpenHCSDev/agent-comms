"""Page queries prepare owned offsets but never rewrite published archives."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest

from agent_comms.bus_page_index import BusPageIndex, BusPageRow, StaleBusPageIndexError
from agent_comms.comms import Comms
from agent_comms.message_page import MessagePageRequest
from agent_comms.read_basis import AllChannelDisplayScope
from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.messages import InfoKind, Message
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from agent_comms.wire_metadata import ArchivedAccess, WritableAccess
from agent_comms.wire_log import (CurrentSnapshotOpening, OpenedWireSnapshot, SavedPrefixOpening,
                                  WireSnapshot)


def retained_source(root, count, *, access=ArchivedAccess()):
    comms = Comms(root)
    for name in ("a", "b", "c"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(root)))
    comms.messaging.initialize_private_initial_protocol()
    with _store_lock(comms.root / 'wire'), comms.bus.log.locked():
        with comms.bus.log.path.open("w") as output:
            for seq in range(1, count + 1):
                row = Message("a", "b" if seq % 3 == 0 else "c", f"m{seq}", InfoKind, seq=seq)
                output.write(json.dumps(row.to_wire()) + "\n")
        comms.bus.log.path.chmod(0o600)
        marker = comms.bus.log.read_metadata_unlocked(required=True)
        marker.last_seq = marker.claimed_after_seq = count
        marker.checkpoint_version = marker.checkpoint_seal = None
        comms.bus.log.write_metadata_unlocked(marker)
        (root / "private_bus_checkpoint.sqlite3").unlink()
        install_private_bus_checkpoint(comms.bus.log, _bus_locked=True)
    with comms.bus.log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as opened:
        with BusPageIndex(comms.bus.log.path) as index:
            assert index.covers(opened)
    marker = comms.bus.log.read_metadata_unlocked(required=True)
    marker.access = access
    comms.bus.log.write_metadata_unlocked(marker)
    return comms.bus


def test_page_queries_require_owned_offsets_and_leave_archives_unchanged(tmp_path):
    empty = Comms(tmp_path / 'empty').bus
    assert empty.log.read_page(MessagePageRequest(AllChannelDisplayScope('#any'))).messages == ()
    assert not (empty.log.path.parent / 'bus_page_index.sqlite3').exists()

    bus = retained_source(tmp_path / 'owned', 9, access=WritableAccess())
    path = bus.log.path.with_name('bus_page_index.sqlite3')
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(f"UPDATE {BusPageRow.declared_name} SET offset=0 WHERE seq=9")
    with pytest.raises(StaleBusPageIndexError):
        bus.incoming_page('b', after=6)
    with BusPageIndex(bus.log.path) as index:
        index.invalidate()
    assert [message.seq for message in bus.incoming_page('b', after=6).messages] == [9]
    for member in CoordinationStore.files(path):
        member.unlink(missing_ok=True)
    assert [message.seq for message in bus.incoming_page('b', after=6).messages] == [9]
    assert path.exists()
    for member in CoordinationStore.files(path):
        member.unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as db, db:  # an earlier declaration
        db.execute(f'CREATE TABLE "{BusPageRow.declared_name}" ("seq" INTEGER NOT NULL, '
                   '"offset" INTEGER NOT NULL, PRIMARY KEY ("seq")) STRICT')
    with pytest.raises(StaleBusPageIndexError):
        bus.incoming_page('b', after=6)

    archive = retained_source(tmp_path / 'archive', 9)
    assert [message.seq for message in archive.incoming_page('b', after=6).messages] == [9]
    path = archive.log.path.with_name('bus_page_index.sqlite3')
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(f"UPDATE {BusPageRow.declared_name} SET offset=0 WHERE seq=9")
    original = (path.read_bytes(), archive.log.path.read_bytes())
    with pytest.raises(StaleBusPageIndexError):
        archive.incoming_page('b', after=6)
    assert (path.read_bytes(), archive.log.path.read_bytes()) == original


@pytest.mark.parametrize("opening", [SavedPrefixOpening(), CurrentSnapshotOpening()])
def test_references_decode_only_their_indexed_rows(tmp_path, monkeypatch, opening):
    bus = retained_source(tmp_path / 'sparse', 200)
    with bus.log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as opened:
        wanted = [message for message, _ in opened.public_records() if message.seq in (7, 150)]
    decoded = []
    from agent_comms.wire_record import WireRecord

    decode_record = WireRecord.public_from_wire.__func__

    def record_decode(cls, value):
        record = decode_record(cls, value)
        decoded.extend(message.seq for message in record.messages())
        return record

    monkeypatch.setattr(WireRecord, 'public_from_wire', classmethod(record_decode))
    with bus.log.opened_wire_snapshot(opening=opening) as opened:
        assert type(opened) is OpenedWireSnapshot
        references = tuple(message.reference for message in wanted)
        assert opened.messages_for_references(references) == tuple(wanted)
    assert decoded == [7, 150]


def test_parallel_readers_receive_repaired_offsets_once(tmp_path, monkeypatch):
    bus = retained_source(tmp_path / 'race', 9, access=WritableAccess())
    with bus.log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as opened:
        message, _ = next(opened.public_records())
    with BusPageIndex(bus.log.path) as index:
        index.invalidate()
    readers_ready = threading.Barrier(2)
    sync = BusPageIndex.sync
    repairs = []

    def record_repair(index, opened):
        repairs.append(opened.revision)
        return sync(index, opened)

    monkeypatch.setattr(BusPageIndex, 'sync', record_repair)

    def read_reference():
        with bus.log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as opened:
            with BusPageIndex(bus.log.path) as index:
                assert index.covers(opened)
            readers_ready.wait(timeout=5)
            return opened.messages_for_references((message.reference,))

    with ThreadPoolExecutor(max_workers=2) as executor:
        readers = (executor.submit(read_reference), executor.submit(read_reference))
        for reader in readers:
            assert reader.result(timeout=5) == (message,)
    assert len(repairs) == 1
    with BusPageIndex(bus.log.path) as index:
        with bus.log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as opened:
            assert index.covers(opened)


def test_append_commits_while_an_older_page_snapshot_is_open(tmp_path):
    bus = retained_source(tmp_path / 'wal-reader', 9, access=WritableAccess())
    with bus.log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as opened:
        with BusPageIndex(bus.log.path) as index, index.covered_read(opened):
            with ThreadPoolExecutor(max_workers=1) as executor:
                message = executor.submit(Comms(bus.log.path.parent).messaging.send,
                                          'a', 'b', 'published with a page open').result(timeout=3).message
            assert len(BusPageRow.select(index.connection)) == 9
    assert message in bus.incoming_page('b', after=0).messages
