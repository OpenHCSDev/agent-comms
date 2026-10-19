"""Actual process birth is the registration/claim_scope authority, never PID alone."""

import os
import subprocess
import sys
from dataclasses import replace

import pytest

from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.cli_bridges.shared import IncomingFrame
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.messages import InfoKind, Message
from agent_comms.registry_document import RegistryDocument
from agent_comms.runtime_requests import ProjectRuntimeRequest
from agent_comms.thread_execution import ExternalThreadExecution, ThreadExecution
from agent_comms.thread_status import ConnectedThreadStatus, NoBridgeThreadStatus
from agent_comms.threads import Thread


def test_claim_attach_and_reload_preserve_the_captured_process(tmp_path):
    comms = wire(tmp_path)
    owner = comms.threads.claim_thread(
        "owner", tags=frozenset(), worktree=str(tmp_path), process=ProcessIdentity.capture(os.getpid())
    )
    identity = ProcessIdentity.capture(os.getpid())
    assert owner.process_identity == identity
    session = tmp_path / "session.jsonl"
    session.write_text('{"type":"session","id":"fixture"}\n')
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
    assert saved.process_identity == NoProcess() and not saved.active_turn.executing
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
    assert current.process_identity == identity and current.active_turn == leased.active_turn
    comms.registry.release_turn(current.turn_lease)


class UnstartedThreadExecution(ThreadExecution, declared_name=None):
    """An inbox no delivered message can wake."""


def test_external_turn_start_is_declared_at_registration_and_a_dead_bridge_reads_no_bridge(tmp_path):
    comms = wire(tmp_path)
    child = subprocess.Popen((sys.executable, "-c", "import time; time.sleep(60)"))
    try:
        bridge = ProcessIdentity.capture(child.pid)
        cli = comms.registry.declare(comms.registry.new_thread(
            "cli", frozenset(), str(tmp_path), execution=ExternalThreadExecution, process_identity=bridge))
        assert comms.registry.snapshot().status("cli") == ConnectedThreadStatus()
        message = Message(sender="lead", target="cli", body="wake up", type=InfoKind)
        assert cli.execution.turn_start(cli)(message) == IncomingFrame(message).text
    finally:
        child.kill()
        child.wait()
    assert comms.registry.snapshot().status("cli") == NoBridgeThreadStatus()
    with pytest.raises(RelationViolationError, match="'silent' declares no turn start"):
        comms.registry.declare(comms.registry.new_thread(
            "silent", frozenset(), str(tmp_path), execution=UnstartedThreadExecution))
