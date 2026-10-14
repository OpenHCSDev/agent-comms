"""Route projection decoding and committed reads do not share writer custody."""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from agent_comms import bus_route_counts as route_module
from agent_comms.bus_route_counts import BusRouteCounts
from agent_comms.comms import wire
from agent_comms.routing import DeliveryMessage
from agent_comms.threads import Thread


def _sync(comms, decode=None):
    with comms.bus.log._opened_wire_snapshot(need_sequence=False) as source:
        root_id = source.metadata.root_id
        return BusRouteCounts(comms.bus.log.path).sync(
            source.stream,
            source.revision,
            decode or (lambda record: DeliveryMessage.from_wire(record, root_id=root_id)),
        )


def _case(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("sender", frozenset(), str(tmp_path)))
    comms.registry.declare(Thread("recipient", frozenset(), str(tmp_path)))
    comms.messaging.send_message("sender", "recipient", "first")
    assert _sync(comms).complete()
    return comms


def test_reader_returns_committed_behind_while_writer_decodes_without_lock(tmp_path):
    comms = _case(tmp_path)
    comms.messaging.send_message("sender", "recipient", "appended")
    entered = threading.Event()
    release = threading.Event()
    with comms.bus.log._opened_wire_snapshot(need_sequence=False) as opened:
        root_id = opened.metadata.root_id

    def decode(record):
        entered.set()
        assert release.wait(5)
        return DeliveryMessage.from_wire(record, root_id=root_id)

    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(_sync, comms, decode)
        assert entered.wait(5)
        with comms.bus.log._opened_wire_snapshot(need_sequence=False) as source:
            started = time.perf_counter()
            projected = BusRouteCounts(comms.bus.log.path).read(
                source.stream,
                source.revision,
                lambda connection: BusRouteCounts.routes(connection),
                [],
            )
            elapsed = time.perf_counter() - started
        assert not projected.coverage.complete()
        assert len(projected.committed) == 1
        release.set()
        assert writer.result(5).complete()
    assert elapsed < 0.05


def test_changed_checkpoint_rejects_stale_batch_without_duplicate_rows(tmp_path):
    comms = _case(tmp_path)
    comms.messaging.send_message("sender", "recipient", "one append")
    entered = threading.Event()
    release = threading.Event()
    with comms.bus.log._opened_wire_snapshot(need_sequence=False) as opened:
        root_id = opened.metadata.root_id

    def delayed(record):
        entered.set()
        assert release.wait(5)
        return DeliveryMessage.from_wire(record, root_id=root_id)

    with ThreadPoolExecutor(max_workers=1) as executor:
        stale = executor.submit(_sync, comms, delayed)
        assert entered.wait(5)
        assert _sync(comms).complete()
        release.set()
        assert stale.result(5).complete()

    index = BusRouteCounts(comms.bus.log.path)
    with index.sqlite_reader() as connection:
        totals = index.routes(connection)
    assert sum(row.total for row in totals) == 2


def test_reader_is_query_only_and_one_row_commit_has_short_lock(tmp_path, monkeypatch):
    comms = _case(tmp_path)
    index = BusRouteCounts(comms.bus.log.path)
    with index.sqlite_reader() as connection:
        assert connection.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM route_total_row")

    held = []
    original = route_module._store_lock

    @contextmanager
    def measured(*args, **kwargs):
        with original(*args, **kwargs) as lock:
            started = time.perf_counter()
            yield lock
            held.append(time.perf_counter() - started)

    monkeypatch.setattr(route_module, "_store_lock", measured)
    comms.messaging.send_message("sender", "recipient", "timed append")
    assert _sync(comms).complete()
    assert held and max(held) < 0.005
