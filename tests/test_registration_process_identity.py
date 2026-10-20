"""Actual process birth is the registration/claim_scope authority, never PID alone."""

import os
from dataclasses import replace

import pytest

from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.registry_document import RegistryDocument
from agent_comms.runtime_requests import ProjectRuntimeRequest
from agent_comms.threads import Thread
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.maintenance_barrier import (
    InstallingPhase, MaintenanceOperator, PausedPhase, ReadyPhase,
)
from agent_comms.thread_status import NoBridgeThreadStatus


@pytest.mark.parametrize('phases', [(), (PausedPhase,), (PausedPhase, InstallingPhase)])
def test_bridge_releases_during_maintenance_without_allowing_a_new_owner(tmp_path, phases):
    comms = wire(tmp_path)
    registry = comms.registry
    registry.declare(registry.new_thread(
        'bridge', frozenset(), str(tmp_path), execution=ExternalThreadExecution,
    ))
    gate = comms.owners.maintenance
    process = ProcessIdentity.capture(os.getpid())
    with registry.external_process('bridge', process):
        hold = gate.begin(MaintenanceOperator.current())
        for phase in phases:
            hold = gate.advance(hold, phase)
        with pytest.raises(RelationViolationError, match='Maintenance'):
            registry.register(registry.require('bridge'), new_owner=True)
    assert registry.require('bridge').process_identity == NoProcess()
    assert registry.status('bridge') == NoBridgeThreadStatus()
    assert gate.read() == hold
    with pytest.raises(RelationViolationError, match='Maintenance'):
        with registry.external_process('bridge', process):
            pytest.fail('A new bridge entered during maintenance')
    while hold.phase is not ReadyPhase:
        hold = gate.advance(hold, hold.phase.successors()[0])
    with registry.external_process('bridge', process):
        assert registry.require('bridge').process_identity == process
    assert registry.require('bridge').process_identity == NoProcess()


def test_claim_attach_and_reload_preserve_the_captured_process(tmp_path):
    comms = wire(tmp_path)
    owner = comms.threads.claim_thread(
        "owner", tags=frozenset(), worktree=str(tmp_path), process=ProcessIdentity.capture(os.getpid())
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
    with pytest.raises(RelationViolationError, match="no owner process"):
        unbound.require_process()
    rebound = comms.threads.attach_session(unbound, str(session), process=identity)
    assert rebound.process_identity == identity
    restored = RegistryDocument()
    restored.restore_stopped(comms.registry.snapshot(), (owner.name,))
    saved = restored.threads[owner.name]
    assert saved.process_identity == NoProcess() and not saved.turn_record.read().executing
    assert saved.incarnation == owner.incarnation and saved.session_file == str(session)


def test_same_pid_different_birth_advances_both_authority_domains(tmp_path):
    comms = wire(tmp_path)
    identity = ProcessIdentity.capture(os.getpid())
    owner = comms.registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=identity)
    comms.registry.declare(owner)
    before = comms.registry.snapshot()
    stale = replace(identity, start_time=identity.start_time + 1)
    comms.registry.register(replace(owner, process_identity=stale))
    after = comms.registry.snapshot()
    assert after.owner_generations[owner.name] > before.owner_generations[owner.name]
    assert after.claimed_generations[owner.name] > before.claimed_generations[owner.name]
    assert comms.registry.require(owner.name).process_identity.runs_here()
    with pytest.raises(RelationViolationError, match="live owner"):
        comms.registry.live_owner_with_claim(owner.name)
    with pytest.raises(RelationViolationError, match="live owner"):
        comms.registry.lease_local_turn(owner.name, "reused-pid")
    with pytest.raises(RelationViolationError, match="does not own"):
        comms.threads.rename_managed_thread(owner.name, "renamed", owner_pid=os.getpid())
    assert identity.alive()


def test_project_request_entry_retains_original_birth_across_rename(tmp_path):
    comms = wire(tmp_path)
    owner = comms.registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.declare(owner)
    request = ProjectRuntimeRequest.for_native(
        comms.registry.snapshot(), comms.registry.require("owner")
    )
    assert request.require_original(comms.registry.entry("owner").entry).name == "owner"
    comms.registry.rename("owner", "renamed")
    current = comms.registry.entry("renamed").entry
    assert request.require_original(current).name == "renamed"
    with pytest.raises(ValueError, match="another original"):
        replace(request, thread="stranger").require_original(current)
    with pytest.raises(RelationViolationError, match="lease changed"):
        request.require_original(replace(current, owner_generation=current.owner_generation + 1))


def test_active_executor_cannot_be_replaced_by_same_pid_different_birth(tmp_path):
    comms = wire(tmp_path)
    identity = ProcessIdentity.capture(os.getpid())
    owner = comms.threads.claim_thread(
        "owner", tags=frozenset(), worktree=str(tmp_path), process=identity
    )
    leased, _ = comms.registry.lease_local_turn(owner.name, "active-turn")
    stale = replace(identity, start_time=identity.start_time + 1)
    with pytest.raises(RelationViolationError, match="replace an executor"):
        comms.registry.declare(replace(owner, process_identity=stale))
    assert comms.registry.require(owner.name) == leased
    comms.registry.declare(comms.registry.new_thread('owner', frozenset(), str(tmp_path), title='metadata'))
    current = comms.registry.require(owner.name)
    assert current.process_identity == identity and current.turn_record == leased.turn_record
    assert current.turn_record.read().id == 'active-turn'
    comms.registry.release_turn(current.turn_lease)
