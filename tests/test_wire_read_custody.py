"""Bus snapshots share read custody while publication remains exclusive."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from agent_comms.comms import wire
from agent_comms.threads import Thread


def test_opened_readers_overlap_and_delivery_waits_only_for_their_source_capture(
    tmp_path, monkeypatch
):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("sender", frozenset(), str(tmp_path)))
    comms.registry.declare(Thread("recipient", frozenset(), str(tmp_path)))
    first = comms.messaging.send_message("sender", "recipient", "first")
    log = comms.bus.log

    original_marker = log._private_marker_unlocked
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

    monkeypatch.setattr(log, "_private_marker_unlocked", pause_source_capture)

    def capture():
        with log._opened_wire_snapshot() as opened:
            return opened.through, opened.boundary

    with ThreadPoolExecutor(max_workers=3) as executor:
        readers = (executor.submit(capture), executor.submit(capture))
        assert readers_entered.wait(timeout=5), "bus readers serialized behind each other"
        delivery = executor.submit(
            comms.messaging.send_message, "sender", "recipient", "after snapshots"
        )
        time.sleep(0.05)
        assert not delivery.done(), "publication entered while shared source capture was active"
        release_readers.set()
        cuts = tuple(reader.result(timeout=5) for reader in readers)
        second = delivery.result(timeout=5)

    assert cuts[0] == cuts[1]
    assert cuts[0][0] == first.seq
    assert second.seq == first.seq + 1
    assert [message.body for message in comms.bus.inbox("recipient")] == [
        "first",
        "after snapshots",
    ]
