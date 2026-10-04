"""Real release-before-exit: retirement keeps checking the exact owner fence."""

import os
import signal
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.child_process import (
    ExitedOutcome,
    ObservedProcess,
    ParentedProcess,
    SignaledOutcome,
)
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.owner_lifecycle import OwnerRestartSelection


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < deadline, f"Owner did not produce {path.name}"
        time.sleep(0.01)


@pytest.fixture
def releasing_owner(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Release handler uses POSIX TERM")
    root = tmp_path / "wire"
    script = tmp_path / "owner.py"
    script.write_text("""import os, signal, time
from pathlib import Path
from agent_comms.comms import Comms
from agent_comms.threads import current_thread
root = Path(os.environ['AGENT_COMMS_ROOT'])
comms = Comms(root)
def release(signum, frame):
    comms.owners.release('worker')
    (root / 'released').touch()
    while not (root / 'exit').exists(): time.sleep(0.01)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, release)
comms.registry.declare(current_thread())
(root / 'ready').touch()
while True: time.sleep(0.01)
""")
    env = {
        **os.environ,
        "AGENT_COMMS_ROOT": str(root),
        "AGENT_COMMS_THREAD": "worker",
        "PI_AGENT_ID": "worker",
        "PI_WORKTREE": str(tmp_path),
        # This real source process must declare the command it actually runs.
        # Restart capture cannot infer extinct launch arguments from its PID.
        "AGENT_COMMS_AGENT_BIN": sys.executable,
        "AGENT_COMMS_AGENT_ARGS": shlex.join((str(script),)),
    }
    child = ParentedProcess.launch((sys.executable, str(script)), env=env)
    comms = Comms(root)
    try:
        wait_for(root / "ready")
        yield comms, child
    finally:
        # A release handler deliberately resists TERM to exercise escalation.
        if child.alive():
            child.force()
        child.reap()
        current = comms.registry.snapshot().threads.get("worker")
        if (
            current is not None
            and current.process_alive
            and current.process_identity != child.identity
        ):
            ObservedProcess(current.process_identity).stop_sync()


@pytest.mark.parametrize("mode", ["restart", "guarded", "stop"])
def test_released_process_must_exit_before_replacement(releasing_owner, mode):
    comms, child = releasing_owner
    original = comms.registry.require("worker")
    arguments = {}
    if mode == "guarded":
        snapshot = comms.registry.snapshot()
        arguments["expected"] = OwnerRestartSelection.capture(snapshot, "worker")
    if mode == "stop":
        comms.owners.stop("worker")
    else:
        (result,) = comms.owners.restart_owners(
            ["worker"], agent_bin=sys.executable, agent_args=["-c", "print('local')"], **arguments
        )
        replacement = comms.registry.require("worker")
        assert replacement.process_alive
        assert replacement.process_identity != original.process_identity
        assert result.previous_pid == original.pid
    assert (comms.root / "released").exists()
    assert not child.alive()
    assert child.reap() == SignaledOutcome(signal.SIGKILL)
    receipt = comms.owners.releases.read()["worker"]
    assert receipt.thread.process_identity == original.process_identity
    assert receipt.after > receipt.before


def test_voluntary_exit_during_grace_is_reaped_without_force(releasing_owner):
    comms, child = releasing_owner
    with ThreadPoolExecutor(max_workers=1) as pool:
        stopping = pool.submit(comms.owners.stop, "worker")
        wait_for(comms.root / "released")
        (comms.root / "exit").touch()
        stopping.result(timeout=5)
    assert child.reap() == ExitedOutcome(0)
    assert not child.alive()


@pytest.mark.parametrize("change", ["birth", "admission", "receipt"])
def test_changed_owner_after_release_refuses_escalation_and_replacement(releasing_owner, change):
    comms, child = releasing_owner
    with ThreadPoolExecutor(max_workers=1) as pool:
        stopping = pool.submit(comms.owners.restart_owners, ["worker"], agent_bin=sys.executable)
        wait_for(comms.root / "released")
        current = comms.registry.require("worker")
        if change == "birth":
            comms.registry.register(
                replace(
                    current,
                    process_identity=replace(
                        child.identity, start_time=child.identity.start_time + 1
                    ),
                ),
                new_owner=True,
            )
        elif change == "admission":
            comms.registry.register(current, new_owner=True)
        else:
            comms.owners.releases.replace({})
        with pytest.raises(RelationViolationError, match="changed while stopping"):
            stopping.result(timeout=5)
        assert child.alive(), "Stale fence must not authorize forced retirement"
