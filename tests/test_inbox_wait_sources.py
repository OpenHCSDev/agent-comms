"""An inbox wait wakes on the bus's own commits, not on every other store in its root (P1200)."""

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from agent_comms.messaging.agent_inbox import SecondsWait
from agent_comms.messaging.bus import Bus


def waited(tmp_path: Path, touched: str) -> int:
    """How many times a 2 s inbox wait re-read its page while ``touched`` changed under it."""
    bus = Bus(Bus.path_in(tmp_path))
    for source in bus.wake_sources():
        source.path.parent.mkdir(parents=True, exist_ok=True)
        source.path.touch()
    reads = 0

    def page():
        nonlocal reads
        reads += 1
        return SimpleNamespace(page=SimpleNamespace(messages=()))

    def write() -> None:
        time.sleep(0.3)
        (tmp_path / touched).write_text(str(time.time()))

    writer = threading.Thread(target=write)
    writer.start()
    SecondsWait(2).read(bus, page)
    writer.join()
    return reads


def test_a_heartbeat_or_goal_write_does_not_rerun_a_waiting_inbox_read(tmp_path: Path) -> None:
    """Every cloud watch waits like this, so each such write used to cost one full read per socket."""
    assert waited(tmp_path, "service_passes.json") == 1
    assert waited(tmp_path, "changed/goal_waits.changed") == 1


def test_a_bus_commit_still_wakes_the_wait(tmp_path: Path) -> None:
    assert waited(tmp_path, "changed/bus.changed") >= 2
