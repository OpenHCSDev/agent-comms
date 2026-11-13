"""Real release-before-exit: a host's retirement keeps checking the exact owner fence."""

import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.execution.launch_runtime import RestoredRuntime
from agent_comms.execution.owner_cutover import PreserveOwnerRuntime
from agent_comms.execution.owner_lifecycle import OwnerLifecycle, OwnerRestartSelection
from agent_comms.execution.owner_restart import (
    FencedOwnerBatch,
    OwnerRestartRequest,
    StoppedOwnerFailure,
)
from agent_comms.execution.pi_vocabulary import OffThinkingLevel
from agent_comms.execution.restart_scope import OwnersNamed
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.owner_release import NoRelease
from agent_comms.registry.thread_model import DeclaredModel
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.store_files import ExclusiveLock, StoreLockContention, _store_lock
from bus_record_fixture import configure_local_native, prepare_native_comms, start_source_owner


def source_runtime() -> RestoredRuntime:
    return RestoredRuntime(Path(sys.executable))


def restart_worker(owners: OwnerLifecycle, **arguments):
    return PreserveOwnerRuntime().restart(owners, OwnerRestartRequest(
        OwnersNamed(("worker",)), launch_runtime=source_runtime(), **arguments,
    ))


@pytest.fixture
def hosted_owner(tmp_path, monkeypatch):
    """A real host holding worker; nothing here is prompted, so its provider is never dialed."""
    configure_local_native(
        tmp_path / "config", monkeypatch, provider="release-local", port=9, models=("fixture",),
    )
    comms = prepare_native_comms(wire(tmp_path / "wire"))
    project = tmp_path / "project"
    project.mkdir()
    comms.registry.declare(NativeThread(
        "worker", frozenset(), Checkout(project),
        model=DeclaredModel("release-local/fixture"), thinking_level=OffThinkingLevel,
    ))
    start_source_owner(comms, "worker", source_runtime())
    original = comms.registry.require("worker")
    yield comms, original


def test_stop_before_first_worker_keeps_the_exact_owner_fence(tmp_path):
    from agent_comms.execution.child_process import (
        NoProcess,
        ProcessIdentity,
    )

    comms = wire(tmp_path / "wire")
    declared = comms.registry.declare(NativeThread('worker', frozenset(), Checkout(tmp_path)))
    generation = comms.registry.snapshot().claim_generation(declared.name)
    comms.owners.stop(declared.name)
    snapshot = comms.registry.snapshot()
    assert snapshot.statuses[declared.name].is_stopped()
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


def test_stop_of_crashed_owner_records_exact_release(hosted_owner):
    comms, original = hosted_owner
    os.kill(original.require_process().pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while original.process_alive:
        assert time.monotonic() < deadline, "The killed host stayed alive"
        time.sleep(0.01)
    comms.owners.stop("worker")
    snapshot = comms.registry.snapshot()
    acknowledgement = comms.owners.releases.read_many(("worker",))["worker"]
    assert acknowledgement.current(snapshot, original, acknowledgement.before)
    assert snapshot.statuses["worker"].is_stopped()


@pytest.mark.parametrize("mode", ["restart", "guarded", "stop"])
def test_released_process_must_exit_before_replacement(hosted_owner, mode):
    comms, original = hosted_owner
    arguments = {}
    if mode == "guarded":
        arguments["expected"] = OwnerRestartSelection.capture(comms.registry.snapshot(), "worker")
    if mode == "stop":
        comms.owners.stop("worker")
    else:
        (result,) = restart_worker(comms.owners, **arguments)
        replacement = comms.registry.require("worker")
        assert replacement.process_alive
        assert replacement.process_identity != original.process_identity
        assert result.previous_pid == original.require_process().pid
    assert not original.process_alive
    acknowledgement = comms.owners.releases.read_many(("worker",))["worker"]
    assert acknowledgement.process_identity == original.process_identity
    assert acknowledgement.after > acknowledgement.before


class OwnerChangesAfterRetirement(OwnerLifecycle):
    """A real retirement, then a real registry edit before the restart reads the release."""

    def __init__(self, comms, change) -> None:
        super().__init__(comms.root, comms.registry, comms.bus, comms.goals, comms.messaging)
        self.change = change

    def _stop_processes(self, owners) -> None:
        super()._stop_processes(owners)
        self.change(self, self.registry.require("worker"))


def rebirth(owners: OwnerLifecycle, current) -> None:
    released = owners.releases.read_many(("worker",))["worker"].process_identity
    owners.registry.register(replace(
        current, process_identity=replace(released, start_time=released.start_time + 1),
    ), new_owner=True)


def reclaim(owners: OwnerLifecycle, current) -> None:
    owners.registry.register(current, new_owner=True)


def forget_release(owners: OwnerLifecycle, current) -> None:
    owners.releases.remove_threads((current,))


@pytest.mark.parametrize(("change", "words"), [
    (rebirth, "Completed retirement differs from the selected original owner"),
    (reclaim, "Completed retirement differs from the selected original owner"),
    (forget_release, "Completed owner retirement is missing"),
])
def test_changed_owner_after_release_refuses_replacement(hosted_owner, change, words):
    comms, original = hosted_owner
    with pytest.raises(StoppedOwnerFailure) as caught:
        restart_worker(OwnerChangesAfterRetirement(comms, change))
    failure = caught.value
    assert isinstance(failure.__cause__, RelationViolationError)
    # Toad shows a RuntimeError on the thread; its text names the cause.
    assert isinstance(failure, RuntimeError) and words in str(failure)
    phase = failure.stopped
    assert isinstance(phase, FencedOwnerBatch)
    assert len(phase.captured) == len(phase.unconfirmed) == 1
    assert phase.captured[0][0].process_identity == original.process_identity
    assert phase.retired == ()
    assert phase.launches[0].process == original.process_identity
    assert not comms.registry.require("worker").process_alive, "A stale fence launched a replacement"
    with _store_lock(comms.bus.bus.path, ExclusiveLock(), StoreLockContention(0)):
        pass  # the failed restart released the wire itself
