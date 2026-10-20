"""Bus snapshots share read ownership while publication remains exclusive."""

import threading
from concurrent.futures import ThreadPoolExecutor

from agent_comms.bus_page_index import BusPageIndex, BusPageSource
from agent_comms.comms import wire
from agent_comms.wire_log import CurrentSnapshotOpening


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


