"""The real loop owner restarts a crashed thread and its hook sees kernel lifetimes."""
import io
import json
import subprocess
import sys
import threading

from agent_comms.commander.loops import Loop, ThreadOutput
from agent_comms.todos import TodoConflict
from agent_comms_hooks.watch import LoopsClaim, LoopsRecord, WatchRecord
from test_commander_problem_orders import create


def test_crash_traceback_peer_lifetime_guard_and_rearm(tmp_path, monkeypatch, commander):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Loop, "members", {})
    failed, healthy, recovered, release = (threading.Event() for _ in range(4))
    todo = create(commander, "P1")

    class Crashing(Loop, name="crashing-test"):
        def run(self):
            if not failed.is_set():
                failed.set()
                raise TodoConflict(todo)
            print("recovered", flush=True)
            recovered.set()
            release.wait(5)

    class Healthy(Loop, name="healthy-test"):
        def run(self):
            print("peer is still running", flush=True)
            healthy.set()
            release.wait(5)

    sink = io.StringIO()
    router = ThreadOutput(sink)
    crashing, peer = Crashing(), Healthy()
    crashing.output = peer.output = sink
    record = WatchRecord("commander", "/bus", ("agent-comms-commander", "loops"))
    group = LoopsRecord(record.bus, record.root, record.command, tuple(Loop.members))

    def hook():
        result = subprocess.run(
            [sys.executable, "-c", "from agent_comms_hooks.watch import main; main()"],
            input=json.dumps({"session_id": "session", "hook_event_name": "PostToolUse"}),
            text=True, capture_output=True, check=True,
        )
        return result.stdout

    monkeypatch.setattr(sys, "stdout", router)
    monkeypatch.setattr(sys, "stderr", router)
    with LoopsClaim("session").held(group):
        try:
            crashing.start("session", router, record)
            peer.start("session", router, record)
            assert failed.wait(2) and healthy.wait(2)
            crashing.thread.join(2)
            assert not crashing.thread.is_alive() and peer.thread.is_alive()
            warning = json.loads(hook())["hookSpecificOutput"]["additionalContext"]
            assert "crashing-test" in warning and "healthy-test" not in warning
            crashing.start("session", router, record)
            assert recovered.wait(2)
            assert hook() == ""
            output = sink.getvalue()
            assert "TodoConflict: Todo P1 changed" in output
            assert "Traceback (most recent call last)" in output
            assert "[healthy-test " in output and "peer is still running" in output
            assert all(line.startswith(("[crashing-test ", "[healthy-test "))
                       for line in output.splitlines())
        finally:
            release.set()
            crashing.thread.join(2)
            peer.thread.join(2)


def test_unrelated_threads_keep_the_original_output():
    sink = io.StringIO()
    router = ThreadOutput(sink)
    worker = threading.Thread(target=lambda: router.write("ordinary background output\n"))
    worker.start()
    worker.join(2)
    assert sink.getvalue() == "ordinary background output\n"
