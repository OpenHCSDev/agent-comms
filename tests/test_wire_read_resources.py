"""Bus snapshots share read ownership while publication remains exclusive."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from agent_comms.bus_page_index import BusPageIndex, BusPageSource
from agent_comms.comms import wire
from agent_comms.threads import Thread
from agent_comms.wire_record import WireRecord
from agent_comms.wire_log import CurrentSnapshotOpening


def test_opened_readers_overlap_and_delivery_waits_only_for_their_source_capture(
    tmp_path, monkeypatch
):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('sender', frozenset(), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('recipient', frozenset(), str(tmp_path)))
    first = comms.messaging.send("sender", "recipient", "first").message
    log = comms.bus.log

    original_marker = log.private_marker_unlocked
    readers_entered = threading.Event()
    release_readers = threading.Event()
    calls_lock = threading.Lock()
    calls = 0

    def pause_source_capture():
        nonlocal calls
        marker = original_marker()
        with calls_lock:
            calls += 1
            pause = calls <= 2
            if calls == 2:
                readers_entered.set()
        if pause:
            assert release_readers.wait(timeout=5)
        return marker

    monkeypatch.setattr(log, "private_marker_unlocked", pause_source_capture)

    def capture():
        with log.opened_wire_snapshot() as opened:
            return opened.through, opened.boundary

    with ThreadPoolExecutor(max_workers=3) as executor:
        readers = (executor.submit(capture), executor.submit(capture))
        assert readers_entered.wait(timeout=5), "bus readers serialized behind each other"
        delivery = executor.submit(
            comms.messaging.send, "sender", "recipient", "after snapshots"
        )
        time.sleep(0.05)
        assert not delivery.done(), "publication entered while shared source capture was active"
        release_readers.set()
        cuts = tuple(reader.result(timeout=5) for reader in readers)
        second = delivery.result(timeout=5).message

    assert cuts[0] == cuts[1]
    assert cuts[0][0] == first.seq
    assert second.seq == first.seq + 1
    assert [message.body for message in comms.bus.inbox("recipient")] == [
        "first",
        "after snapshots",
    ]


def test_page_index_snapshot_keeps_coverage_and_offsets_on_one_revision(tmp_path):
    comms = wire(tmp_path)
    for name in ("sender", "recipient"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    first = comms.messaging.send("sender", "recipient", "first").message
    with comms.bus.log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as opened:
        with BusPageIndex(comms.bus.log.path) as prepared:
            assert prepared.sync(opened)

    update_started = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with BusPageIndex(comms.bus.log.path) as reader, reader.snapshot():
            saved = BusPageSource.one(reader.connection, singleton=1)
            assert saved is not None
            second = comms.messaging.send("sender", "recipient", "second").message

            def sync():
                update_started.set()
                with comms.bus.log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as opened:
                    with BusPageIndex(comms.bus.log.path) as writer:
                        return writer.sync(opened)

            update = executor.submit(sync)
            assert update_started.wait(timeout=5)
            assert update.result(timeout=5), "WAL publication must not wait for a pinned reader"
            rows = tuple(
                reader.offsets(
                    lower=None, upper=None, descending=False,
                    predicate=("1", ()), before_offset=saved.revision.size,
                )
            )
        assert update.result(timeout=5)
    finally:
        executor.shutdown(wait=True)
    assert [row.seq for row in rows] == [first.seq]
    with BusPageIndex(comms.bus.log.path) as current:
        assert [row.seq for row in current.offsets(
            lower=None, upper=None, descending=False, predicate=("1", ())
        )] == [first.seq, second.seq]


def test_relationship_tail_decode_does_not_hold_publication_resources(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    for name in ("sender", "recipient"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    first = comms.messaging.send("sender", "recipient", "first").message
    decoding = threading.Event()
    release = threading.Event()
    original = WireRecord.public_from_wire

    with ThreadPoolExecutor(max_workers=1) as reads, ThreadPoolExecutor(max_workers=1) as writes:
        reader_thread = reads.submit(threading.current_thread).result(timeout=5)

        def pause_decode(record):
            if threading.current_thread() is reader_thread and record.get('seq') == first.seq:
                decoding.set()
                assert release.wait(timeout=5)
            return original(record)

        monkeypatch.setattr(WireRecord, "public_from_wire", pause_decode)
        reader = reads.submit(comms.relationships.snapshot, "recipient")
        assert decoding.wait(timeout=5)
        publication = writes.submit(
            comms.messaging.send, "sender", "recipient", "during decode"
        )
        try:
            second = publication.result(timeout=5).message
        finally:
            release.set()
        reader.result(timeout=5)

    assert second.seq == 2
