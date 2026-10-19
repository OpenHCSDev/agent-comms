"""Route-count writers decode outside locks; readers use committed WAL snapshots."""

from agent_comms.read_basis import NamedViewer
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest

from agent_comms.bus_route_counts import BusRouteCounts, CompletingRouteCounts, RouteIndexRebuilding
from agent_comms.comms import wire
from agent_comms.routing import DeliveryMessage
from agent_comms.threads import Thread
from agent_comms.wire_log import CurrentSnapshotOpening


def _open_source(comms):
    return comms.bus.log.opened_wire_snapshot(opening=CurrentSnapshotOpening())


def _sync(comms, decode=None):
    with _open_source(comms) as source:
        return BusRouteCounts(comms.bus.log.path).sync(
            source,
            decode or (lambda row: DeliveryMessage.from_wire(
                row, root_id=source.metadata.root_id
            )),
        )


def _case(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_comms.candidate_maintenance.schedule_candidate_catchup", lambda *_: None
    )
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('sender', frozenset(), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('recipient', frozenset(), str(tmp_path)))
    comms.messaging.send("sender", "recipient", "first").message
    assert _sync(comms).complete
    return comms


def test_revision_timestamp_change_does_not_redecode_unchanged_bus(tmp_path, monkeypatch):
    comms = _case(tmp_path, monkeypatch)
    path = comms.bus.log.path
    before = path.stat()
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

    assert _sync(
        comms, lambda _row: pytest.fail("unchanged bytes were decoded")
    ).complete


def test_reader_counts_uncommitted_append_while_writer_decodes_without_index_lock(
    tmp_path, monkeypatch
):
    comms = _case(tmp_path, monkeypatch)
    comms.messaging.send("sender", "recipient", "appended").message
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
        with comms.bus.snapshot(viewer=NamedViewer("recipient")) as (basis, source):
            assert comms.bus.pending_counts_opened(source, basis) == {"sender": 2}
        assert [m.body for m in comms.bus.inbox("recipient")] == ["first", "appended"]
        elapsed = time.perf_counter() - started
        release.set()
        assert writer.result(5).complete
    assert elapsed < 0.2
    with comms.bus.snapshot(viewer=NamedViewer("recipient")) as (basis, source):
        assert comms.bus.pending_counts_opened(source, basis) == {"sender": 2}


def test_reader_beyond_rebuild_bound_is_rebuilding(tmp_path, monkeypatch):
    comms = _case(tmp_path, monkeypatch)
    comms.messaging.send("sender", "recipient", "appended").message
    comms.messaging.send("sender", "recipient", "appended again").message
    # One reader sync commits at most one row past the bound; the second
    # row stays beyond it.
    monkeypatch.setattr(BusRouteCounts, "rebuild_bytes", 1)
    with pytest.raises(RouteIndexRebuilding), comms.bus.snapshot(viewer=NamedViewer("recipient")) as (basis, source):
        comms.bus.pending_counts_opened(source, basis)


@pytest.mark.parametrize("read", ("inbox", "pending_counts_opened"))
def test_readers_use_the_bus_projection_to_complete_their_cut(tmp_path, monkeypatch, read):
    comms = _case(tmp_path, monkeypatch)
    for body in ("second", "third"):
        comms.messaging.send("sender", "recipient", body).message
    monkeypatch.setattr(BusRouteCounts, "rebuild_bytes", 1)
    reader = wire(tmp_path, projection=CompletingRouteCounts)

    if read == "inbox":
        assert [message.body for message in reader.bus.inbox("recipient")] == [
            "first", "second", "third"
        ]
    else:
        with reader.bus.snapshot(viewer=NamedViewer("recipient")) as (basis, source):
            assert reader.bus.pending_counts_opened(source, basis) == {"sender": 3}


def test_cli_who_completes_unindexed_history_that_the_ui_reads_unit_by_unit(
    tmp_path, monkeypatch, capsys
):
    import json

    from agent_comms.cli import main

    monkeypatch.setattr(
        "agent_comms.candidate_maintenance.schedule_candidate_catchup", lambda *_: None
    )
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('sender', frozenset(), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('recipient', frozenset(), str(tmp_path)))
    for body in ("first", "second", "third"):
        comms.messaging.send("sender", "recipient", body).message
    assert not BusRouteCounts(comms.bus.log.path).path.exists()
    monkeypatch.setattr(BusRouteCounts, "rebuild_bytes", 1)

    with pytest.raises(RouteIndexRebuilding), comms.bus.snapshot(
        viewer=NamedViewer("recipient")
    ) as (basis, source):
        comms.bus.pending_counts_opened(source, basis)

    assert main(["--root", str(tmp_path), "who"]) == 0
    who = {row["name"]: row["pending"] for row in json.loads(capsys.readouterr().out)["who"]}
    assert who["recipient"] == 3
