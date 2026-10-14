"""Route-count writers decode outside locks; readers use committed WAL snapshots."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest

from agent_comms.bus_route_counts import BusRouteCounts, RouteIndexReady, RouteIndexRebuilding
from agent_comms.comms import wire
from agent_comms.routing import DeliveryMessage
from agent_comms.threads import Thread


def _open_source(comms):
    return comms.bus.log._opened_wire_snapshot(need_sequence=False)


def _sync(comms, decode=None):
    with _open_source(comms) as source:
        return BusRouteCounts(comms.bus.log.path).sync(
            source.stream,
            source.revision,
            decode or (lambda row: DeliveryMessage.from_wire(
                row, root_id=source.metadata.root_id
            )),
        )


def _case(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_comms.candidate_maintenance.schedule_candidate_catchup", lambda *_: None
    )
    comms = wire(tmp_path)
    comms.registry.declare(Thread("sender", frozenset(), str(tmp_path)))
    comms.registry.declare(Thread("recipient", frozenset(), str(tmp_path)))
    comms.messaging.send_message("sender", "recipient", "first")
    assert isinstance(_sync(comms), RouteIndexReady)
    return comms


def test_revision_timestamp_change_does_not_redecode_unchanged_bus(tmp_path, monkeypatch):
    comms = _case(tmp_path, monkeypatch)
    path = comms.bus.log.path
    before = path.stat()
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

    assert isinstance(
        _sync(comms, lambda _row: pytest.fail("unchanged bytes were decoded")),
        RouteIndexReady,
    )


def test_reader_returns_rebuilding_while_writer_decodes_without_index_lock(
    tmp_path, monkeypatch
):
    comms = _case(tmp_path, monkeypatch)
    comms.messaging.send_message("sender", "recipient", "appended")
    entered = threading.Event()
    release = threading.Event()
    with _open_source(comms) as source:
        root_id = source.metadata.root_id

    def decode(row):
        entered.set()
        assert release.wait(5)
        return DeliveryMessage.from_wire(row, root_id=root_id)

    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(_sync, comms, decode)
        assert entered.wait(5)
        started = time.perf_counter()
        with pytest.raises(RouteIndexRebuilding):
            comms.bus.pending_counts("recipient")
        elapsed = time.perf_counter() - started
        release.set()
        assert isinstance(writer.result(5), RouteIndexReady)
    assert elapsed < 0.2
    assert comms.bus.pending_counts("recipient") == {"sender": 2}
