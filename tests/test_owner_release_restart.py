"""Real release-before-exit: retirement keeps checking the exact owner fence."""

from agent_comms.owner_release import NoRelease
from agent_comms.repo import Checkout
from agent_comms.native_arguments import GivenArguments, GivenBinary
from agent_comms.restart_scope import OwnersNamed

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
    SpawnedProcess,
    SignaledOutcome,
)


from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.owner_restart import FencedOwnerBatch, StoppedOwnerFailure
from agent_comms.store_files import ExclusiveLock, StoreLockContention, _store_lock
from agent_comms.threads import NativeThread


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
    script = Path(__file__).with_name('releasing_owner_child.py')
    arguments = (str(script), str(root))
    env = {
        **os.environ,
        "AGENT_COMMS_THREAD": "worker",
        "PI_WORKTREE": str(tmp_path),
        # This real source process must declare the command it actually runs.
        # Restart capture cannot infer extinct launch arguments from its PID.
        "AGENT_COMMS_AGENT_BIN": sys.executable,
        "AGENT_COMMS_AGENT_ARGS": shlex.join(arguments),
    }
    child = SpawnedProcess.launch((sys.executable, *arguments), env=env)
    comms = wire(root)
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


def test_stop_before_first_worker_keeps_the_exact_owner_fence(tmp_path):
    from agent_comms.child_process import NoProcess, ProcessIdentity

    comms = wire(tmp_path / "wire")
    declared = comms.registry.declare(NativeThread('worker', frozenset(), Checkout(tmp_path)))
    generation = comms.registry.snapshot().claim_generation(declared.name)
    comms.owners.stop(declared.name)
    snapshot = comms.registry.snapshot()
    assert snapshot.statuses[declared.name].stopped
    assert snapshot.threads[declared.name].process_identity == NoProcess()
    release = comms.owners.releases.read_many((declared.name,))[declared.name]
    assert isinstance(release, NoRelease)
    assert not release.current(snapshot, declared, generation)
    assert snapshot.claim_generation(declared.name) > generation

    waiting = comms.registry.declare(NativeThread('waiting', frozenset(), Checkout(tmp_path)))
    before = comms.registry.snapshot().claim_generation(waiting.name)
    comms.registry.register(
        replace(waiting, process_identity=ProcessIdentity.capture(os.getpid())),
        new_owner=True,
    )
    with pytest.raises(RelationViolationError):
        comms.registry.snapshot().require_stopping_owner(waiting, before)


def test_stop_of_crashed_owner_records_exact_release(releasing_owner):
    comms, child = releasing_owner
    original = comms.registry.require("worker")
    child.force()
    child.reap()
    assert not original.process_alive
    comms.owners.stop("worker")
    snapshot = comms.registry.snapshot()
    acknowledgement = comms.owners.releases.read_many(("worker",))["worker"]
    assert acknowledgement.current(snapshot, original, acknowledgement.before)
    assert snapshot.statuses["worker"].stopped


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
            OwnersNamed(("worker",)), agent_bin=GivenBinary(sys.executable), agent_args=GivenArguments(("-c", "print('local')",)), **arguments
        )
        replacement = comms.registry.require("worker")
        assert replacement.process_alive
        assert replacement.process_identity != original.process_identity
        assert result.previous_pid == original.require_process().pid
    assert (comms.root / "released").exists()
    assert not child.alive()
    assert child.reap() == SignaledOutcome(signal.SIGKILL)
    acknowledgement = comms.owners.releases.read_many(("worker",))["worker"]
    assert acknowledgement.process_identity == original.process_identity
    assert acknowledgement.after > acknowledgement.before


def test_voluntary_exit_during_grace_is_reaped_without_force(releasing_owner):
    comms, child = releasing_owner
    with ThreadPoolExecutor(max_workers=1) as pool:
        stopping = pool.submit(comms.owners.stop, "worker")
        wait_for(comms.root / "released")
        (comms.root / "exit").touch()
        stopping.result(timeout=5)
    assert child.reap() == ExitedOutcome(0)
    assert not child.alive()


@pytest.mark.parametrize("change", ["birth", "claim_scope", "acknowledgement"])
def test_changed_owner_after_release_refuses_escalation_and_replacement(releasing_owner, change):
    comms, child = releasing_owner
    with ThreadPoolExecutor(max_workers=1) as pool:
        stopping = pool.submit(comms.owners.restart_owners, OwnersNamed(("worker",)), agent_bin=GivenBinary(sys.executable))
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
        elif change == "claim_scope":
            comms.registry.register(current, new_owner=True)
        else:
            comms.owners.releases.remove_threads((current,))
        with pytest.raises(StoppedOwnerFailure) as caught:
            stopping.result(timeout=5)
    failure = caught.value
    assert isinstance(failure.__cause__, RelationViolationError)
    # Toad shows a RuntimeError on the thread; its text names the cause.
    assert isinstance(failure, RuntimeError) and "changed while stopping" in str(failure)
    phase = failure.stopped
    assert isinstance(phase, FencedOwnerBatch)
    assert len(phase.captured) == len(phase.unconfirmed) == 1
    assert phase.captured[0][0].process_identity == child.identity
    assert phase.retired == ()
    assert phase.launches[0].process == child.identity
    assert child.alive(), "Stale fence must not authorize forced retirement"
    assert comms.registry.require("worker").require_process().pid == child.identity.pid
    with _store_lock(comms.bus.bus.path, ExclusiveLock(), StoreLockContention(0)):
        pass  # the failed restart released the wire itself


