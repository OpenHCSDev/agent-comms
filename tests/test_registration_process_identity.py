"""Actual process birth is the registration/claim_scope authority, never PID alone."""

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.comms import wire
from agent_comms.config_options import ConfigOptions
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import NoProcess, ProcessIdentity, SpawnedProcess
from agent_comms.execution.launch_runtime import ThreadLaunch
from agent_comms.execution.session_file import RegistrySessionLocation
from agent_comms.installer.native_build_inputs import DetachedChildStdio
from agent_comms.messaging.bus import Bus
from agent_comms.nominal.field_codec import FieldCodec, TurnId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registry_document import RegistryDocument
from agent_comms.registry.thread_text import GivenThreadText
from agent_comms.registry.threads import ExternalThread, ForkThread, NativeThread
from agent_comms.storage.maintenance_barrier import (
    InstallingPhase,
    MaintenanceHeldError,
    MaintenanceOperator,
    PausedPhase,
)
from agent_comms.transport import runtime_requests
from agent_comms.transport.owner_requests import RetainedOwnerLaunch
from agent_comms.transport.project_request import ProjectRuntimeRequest
from agent_comms.transport.runtime_request import RuntimeRequest
from served_requests import served


def test_cli_worktree_edit_preserves_live_external_owner(tmp_path):
    comms = wire(tmp_path / 'wire')
    Bus.install(Bus.path_in(comms.root))
    identity = ProcessIdentity.capture(os.getpid())
    original = comms.registry.declare(ExternalThread(
        'bridge', frozenset(), Checkout(tmp_path / 'old'),
        process_identity=identity))
    before = comms.registry.snapshot()
    result = subprocess.run(
        (sys.executable, '-m', 'agent_comms.cli', '--root', str(comms.root),
         'register', '--name', original.name, '--worktree', str(tmp_path / 'new')),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    after = comms.registry.snapshot()
    current = after.require(original.name)
    assert current.worktree == Checkout(tmp_path / 'new')
    assert current.process_identity == identity and identity.alive()
    assert current.incarnation == original.incarnation
    # The project edit still fences work issued for the old project, despite
    # retaining the same live bridge process and thread incarnation.
    assert after.owner_generations[original.name] > before.owner_generations[original.name]
    assert after.claim_generation(original.name) > before.claim_generation(original.name)
    # Exact lifecycle replacement still releases a process; omission on a public
    # metadata edit is the operation that preserves it.
    comms.registry.register(replace(current, process_identity=NoProcess()))
    assert comms.registry.require(current.name).process_identity == NoProcess()


@pytest.mark.parametrize('phases', [(), (PausedPhase,), (PausedPhase, InstallingPhase)])
def test_bridge_cannot_write_the_registry_during_maintenance(tmp_path, phases):
    comms = wire(tmp_path)
    registry = comms.registry
    registry.declare(ExternalThread(
        'bridge', frozenset(), Checkout(tmp_path)))
    gate = comms.owners.maintenance
    process = ProcessIdentity.capture(os.getpid())
    with pytest.raises(MaintenanceHeldError):
        with registry.external_process('bridge', process):
            hold = gate.begin(MaintenanceOperator.current())
            for phase in phases:
                hold = gate.advance(hold, phase)
            with pytest.raises(RelationViolationError):
                registry.register(registry.require('bridge'), new_owner=True)
    assert registry.require('bridge').process_identity == process
    assert gate.read() == hold
    with pytest.raises(RelationViolationError):
        with registry.external_process('bridge', process):
            pytest.fail('A new bridge entered during maintenance')


def test_concurrent_starters_reserve_one_current_owner(tmp_path):
    registry = wire(tmp_path).registry
    original = registry.declare(NativeThread("owner", frozenset(), Checkout(tmp_path)))
    title = GivenThreadText("metadata changed during launch preparation")
    registry.register(replace(original, title=title))
    before = registry.snapshot()
    ready = threading.Barrier(2)

    def reserve(process):
        ready.wait(timeout=5)
        with registry.store.editing() as edit:
            return registry.reserve_owner(edit, original, process)

    with ExitStack() as children, ThreadPoolExecutor(max_workers=2) as workers:
        processes = tuple(children.enter_context(SpawnedProcess.launch(
            (sys.executable, "-c", "import sys; sys.stdin.buffer.read()"),
            stdio=DetachedChildStdio(stdin=subprocess.PIPE),
        ).process) for _ in range(2))
        attempts = tuple(workers.submit(reserve, ProcessIdentity.capture(child.pid))
                         for child in processes)
        accepted, refused = [], []
        for attempt in attempts:
            try:
                accepted.append(attempt.result(timeout=10))
            except RelationViolationError as error:
                refused.append(error)
        assert len(accepted) == len(refused) == 1
        (owner,) = accepted
        current = registry.require("owner")
        assert current == owner.thread and current.title == title
        assert current.process_identity in tuple(ProcessIdentity.capture(child.pid)
                                                for child in processes)
        assert owner.claimed_generation == before.claim_generation("owner") + 1
        assert registry.snapshot().claim_generation("owner") == owner.claimed_generation


def test_claim_attach_and_reload_preserve_the_captured_process(tmp_path):
    comms = wire(tmp_path)
    owner = comms.threads.claim_thread(
        "owner", tags=frozenset(), worktree=Checkout(tmp_path), process=ProcessIdentity.capture(os.getpid())
    )
    identity = ProcessIdentity.capture(os.getpid())
    assert owner.process_identity == identity
    session = tmp_path / "session.jsonl"
    session.write_text('{"type":"session","id":"fixture","timestamp":"2026-10-19T11:00:00Z"}\n')
    attached = comms.threads.attach_session(owner, str(session), process=owner.process_identity)
    assert attached.process_identity == identity
    assert wire(tmp_path).registry.require(owner.name) == attached
    unbound = comms.threads.attach_session(attached, str(session), process=NoProcess())
    assert unbound.process_identity == NoProcess()
    with pytest.raises(RelationViolationError):
        unbound.require_process()
    rebound = comms.threads.attach_session(unbound, str(session), process=identity)
    assert rebound.process_identity == identity
    restored = RegistryDocument()
    restored.restore_stopped(comms.registry.snapshot(), (owner.name,))
    saved = restored.threads[owner.name]
    assert saved.process_identity == NoProcess() and not saved.executing
    assert saved.incarnation == owner.incarnation
    assert saved.session_file == RegistrySessionLocation.from_path(str(session), comms.root)


def test_same_pid_different_birth_advances_both_authority_domains(tmp_path):
    comms = wire(tmp_path)
    identity = ProcessIdentity.capture(os.getpid())
    owner = NativeThread('owner', frozenset(), Checkout(tmp_path), process_identity=identity)
    comms.registry.declare(owner)
    before = comms.registry.snapshot()
    stale = replace(identity, start_time=identity.start_time + 1)
    comms.registry.register(replace(owner, process_identity=stale))
    after = comms.registry.snapshot()
    assert after.owner_generations[owner.name] > before.owner_generations[owner.name]
    assert after.claim_generation(owner.name) > before.claim_generation(owner.name)
    assert comms.registry.require(owner.name).process_identity.runs_here()
    with pytest.raises(RelationViolationError):
        comms.registry.live_owner_with_claim(owner.name)
    with pytest.raises(RelationViolationError):
        comms.registry.lease_local_turn(owner.name, TurnId('b777bd9e97665a2b2def8639667b0e72'))
    with pytest.raises(RelationViolationError):
        comms.threads.rename_managed_thread(comms.registry.require(owner.name).incarnation, "renamed", owner=ProcessIdentity.capture(os.getpid()))
    assert identity.alive()


def test_project_request_entry_retains_original_birth_across_rename(tmp_path):
    comms = wire(tmp_path)
    owner = NativeThread('owner', frozenset(), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.declare(owner)
    request = served(ProjectRuntimeRequest.for_native(
        comms.registry.snapshot(), comms.registry.require("owner")
    ))
    assert request.require_original(comms.registry.entry("owner").entry).name == "owner"
    comms.registry.rename("owner", "renamed")
    current = comms.registry.entry("renamed").entry
    assert request.require_original(current).name == "renamed"
    with pytest.raises(ValueError):
        replace(request, thread="stranger").require_original(current)
    with pytest.raises(RelationViolationError):
        request.require_original(replace(current, owner_generation=current.owner_generation + 1))


def test_configured_native_launch_preserves_original_project_binding(tmp_path: Path) -> None:
    comms = wire(tmp_path)
    process = ProcessIdentity.capture(os.getpid())
    owner = comms.registry.declare(NativeThread(
        "owner", frozenset(), Checkout(tmp_path), process_identity=process,
    ))
    snapshot = comms.registry.snapshot()
    configuration = ConfigOptions(RetainedOwnerLaunch.configured(), comms.route)
    environment = configuration.native_environment(owner, snapshot)
    launch = ThreadLaunch.current(environment)
    assert launch.name == owner.name and launch.managed
    assert launch.worktree == owner.worktree
    request = FieldCodec.decode(
        RuntimeRequest, json.loads(environment["AGENT_COMMS_PROJECT_REQUEST"]),
    )
    assert request == ProjectRuntimeRequest.for_native(snapshot, owner)
    assert served(request).require_original(comms.registry.entry(owner.name).entry) == owner
    stale = replace(owner, process_identity=replace(process, start_time=process.start_time + 1))
    with pytest.raises(RelationViolationError):
        configuration.native_environment(stale, snapshot)


@pytest.mark.parametrize("first_module", (
    NativeThread.__module__, ProjectRuntimeRequest.__module__,
    ConfigOptions.__module__, runtime_requests.__name__,
))
def test_native_project_command_cold_contracts(first_module: str) -> None:
    result = subprocess.run(
        (sys.executable, str(Path(__file__).with_name("project_launch_probe.py")), first_module),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_active_executor_cannot_be_replaced_by_same_pid_different_birth(tmp_path):
    comms = wire(tmp_path)
    identity = ProcessIdentity.capture(os.getpid())
    owner = comms.threads.claim_thread(
        "owner", tags=frozenset(), worktree=Checkout(tmp_path), process=identity
    )
    leased, _ = comms.registry.lease_local_turn(
        owner.name, TurnId('e99af36cdfcffbc191cce98c5d771afb')
    )
    stale = replace(identity, start_time=identity.start_time + 1)
    with pytest.raises(RelationViolationError):
        comms.registry.declare(replace(owner, process_identity=stale))
    assert comms.registry.require(owner.name) == leased
    comms.registry.declare(NativeThread('owner', frozenset(), Checkout(tmp_path), title=GivenThreadText('metadata')))
    current = comms.registry.require(owner.name)
    assert current.process_identity == identity and current.turn_record == leased.turn_record
    assert current.turn_record.read().id == 'e99af36cdfcffbc191cce98c5d771afb'
    comms.registry.release_turn(current.turn_lease)


def test_a_fork_row_bound_by_its_own_process_is_an_independent_session(tmp_path):
    """2026-11-04: a session named through the connector, then bound by its own bridge, stayed a
    fork, so `require_actor` refused it as nobody's colleague and nobody's in-turn child."""
    comms = wire(tmp_path)
    registry = comms.registry
    registry.declare(ExternalThread('parent', frozenset(), Checkout(tmp_path)))
    registry.register_child('session', 'parent', ForkThread, comms.goals)
    forked = registry.require('session')
    assert forked.independent_threads() == ()
    with registry.external_process('session', ProcessIdentity.capture(os.getpid())):
        bound = registry.require('session')
        assert bound.independent_threads() == (bound,)
        assert bound.parent == forked.parent
        assert bound.incarnation == forked.incarnation
    assert registry.require('session').independent_threads() == (registry.require('session'),)


def test_a_bound_session_keeps_its_name_on_its_parents_grant_and_no_other(tmp_path):
    """2026-11-04: once bound, the session's own grant was refused the name as "not a declared
    teammate fork", so its calls fell back to the shared grant and were recorded under that."""
    comms = wire(tmp_path)
    registry = comms.registry
    for grant in ('parent', 'stranger'):
        registry.declare(ExternalThread(grant, frozenset(), Checkout(tmp_path)))
    registry.register_child('session', 'parent', ForkThread, comms.goals)
    with registry.external_process('session', ProcessIdentity.capture(os.getpid())):
        bound = registry.require('session')
        assert registry.register_child('session', 'parent', ForkThread, comms.goals) == bound
        assert registry.require('session').process_identity == bound.process_identity
        with pytest.raises(RelationViolationError, match='not a child of'):
            registry.register_child('session', 'stranger', ForkThread, comms.goals)
    with pytest.raises(RelationViolationError, match='not a child of'):
        registry.register_child('parent', 'stranger', ForkThread, comms.goals)
    native = registry.declare(NativeThread('native', frozenset(), Checkout(tmp_path)))
    with pytest.raises(RelationViolationError, match='not a declared teammate fork'):
        registry.register_child(native.name, 'parent', ForkThread, comms.goals)
