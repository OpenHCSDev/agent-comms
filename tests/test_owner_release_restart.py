"""Real release-before-exit fixtures for stopping and replacing owner processes."""

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError


@pytest.fixture
def releasing_owner(tmp_path):
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux environment-backed process identity fixture")
    root = tmp_path / "wire"
    script = tmp_path / "owner.py"
    script.write_text(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "from agent_comms.comms import wire\n"
        "from agent_comms.threads import Thread\n"
        "root = Path(os.environ['AGENT_COMMS_ROOT'])\n"
        "comms = wire(root)\n"
        "def release(signum, frame):\n"
        "    comms.owners.release('worker')\n"
        "    (root / 'released').touch()\n"
        "    while not (root / 'exit').exists(): time.sleep(0.01)\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, release)\n"
        "comms.threads.register(Thread('worker', frozenset({'team'}), str(root), "
        "pid=os.getpid(), session_file=str(root / 'saved.jsonl')))\n"
        "(root / 'ready').touch()\n"
        "while True: time.sleep(0.01)\n"
    )
    env = os.environ.copy()
    env.update(AGENT_COMMS_ROOT=str(root), AGENT_COMMS_THREAD="worker", PI_AGENT_ID="worker")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = subprocess.Popen([sys.executable, str(script)], env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not (root / "ready").exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        yield wire(root), process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def restart_arguments(comms, mode):
    if mode != "guarded":
        return {}
    snapshot = comms.registry.snapshot()
    thread = snapshot.threads["worker"]
    return {
        "expected_incarnations": {
            "worker": (thread.pid, thread.created_at, snapshot.admission_generations["worker"])
        }
    }


def observe_signals_and_launch(comms, process, monkeypatch):
    signals, launches = [], []
    real_signal = comms.owners._signal_local_owner

    def signal_owner(pid, signum):
        signals.append((pid, signum))
        real_signal(pid, signum)

    def launch(thread, agent_bin, agent_args):
        assert not comms.owners._process_alive(process.pid), "release alone is not exit"
        assert process.wait(timeout=1) is not None
        launches.append(thread)
        return replace(thread, pid=process.pid + 1000000)

    monkeypatch.setattr(comms.owners, "_signal_local_owner", signal_owner)
    monkeypatch.setattr(comms.owners, "_launch_owner_unlocked", launch)
    return signals, launches


@pytest.mark.parametrize("mode", ["restart", "guarded", "stop"])
def test_released_process_surviving_grace_is_stopped_before_replacement(
    releasing_owner, monkeypatch, mode
):
    comms, process = releasing_owner
    original = comms.registry.require("worker")
    arguments = restart_arguments(comms, mode)
    signals, launches = observe_signals_and_launch(comms, process, monkeypatch)
    if mode == "stop":
        comms.owners.stop("worker")
    else:
        (result,) = comms.owners.restart_owners(["worker"], **arguments)
        assert result.previous_pid == original.pid
        assert result.pid != original.pid
    assert (comms.root / "released").exists()
    assert signals == [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)]
    assert len(launches) == (0 if mode == "stop" else 1)
    if launches:
        assert launches[0] == original
    assert process.wait(timeout=1) == -signal.SIGKILL


@pytest.mark.parametrize("mode", ["restart", "stop"])
def test_exit_between_grace_and_escalation_needs_no_live_participant_proof(
    releasing_owner, monkeypatch, mode
):
    comms, process = releasing_owner
    signals, launches = observe_signals_and_launch(comms, process, monkeypatch)
    real_wait = comms.owners._wait_for_owner_exit

    def wait_for_exit(pid, seconds):
        result = real_wait(pid, seconds)
        if seconds == 3:
            assert not result and (comms.root / "released").exists()
            (comms.root / "exit").touch()
            assert real_wait(pid, 1)
            return False  # Exit occurred just after the grace observation.
        return result

    monkeypatch.setattr(comms.owners, "_wait_for_owner_exit", wait_for_exit)
    if mode == "stop":
        comms.owners.stop("worker")
    else:
        comms.owners.restart_owners(["worker"])
    assert signals == [(process.pid, signal.SIGTERM)]
    assert len(launches) == (0 if mode == "stop" else 1)
    assert process.wait(timeout=1) == 0


@pytest.mark.parametrize("change", ["pid", "epoch", "receipt", "unverifiable"])
@pytest.mark.parametrize("mode", ["restart", "guarded", "stop"])
def test_release_does_not_authorize_replacement_or_unverifiable_process(
    releasing_owner, monkeypatch, change, mode
):
    comms, process = releasing_owner
    arguments = restart_arguments(comms, mode)
    signals, launches = observe_signals_and_launch(comms, process, monkeypatch)
    real_wait = comms.owners._wait_for_owner_exit

    def wait_for_exit(pid, seconds):
        if seconds != 3:
            return real_wait(pid, seconds)
        deadline = time.monotonic() + 3
        while not (comms.root / "released").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        current = comms.registry.require("worker")
        if change == "pid":
            comms.registry.register(replace(current, pid=current.pid + 1000000), new_owner=True)
        elif change == "epoch":
            comms.registry.register(current, new_owner=True)
        elif change == "receipt":
            path = comms.root / "owner_release_receipts.json"
            receipts = json.loads(path.read_text())
            receipts["worker"]["before"] += 1
            path.write_text(json.dumps(receipts))
        else:
            monkeypatch.setattr(comms.owners, "_is_local_participant", lambda *a, **kw: False)
        return False

    monkeypatch.setattr(comms.owners, "_wait_for_owner_exit", wait_for_exit)
    with pytest.raises(RelationViolationError, match="changed|unverifiable"):
        if mode == "stop":
            comms.owners.stop("worker")
        else:
            comms.owners.restart_owners(["worker"], **arguments)
    assert signals == [(process.pid, signal.SIGTERM)]
    assert launches == []
    assert process.poll() is None
