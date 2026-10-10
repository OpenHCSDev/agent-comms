"""Actual process birth is the registration/admission authority, never PID alone."""

import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.registry_document import RegistryDocument
from agent_comms.threads import Thread


def test_claim_attach_and_reload_preserve_the_captured_process(tmp_path):
    comms = wire(tmp_path)
    owner = comms.threads.claim_thread(
        "owner", tags=frozenset(), worktree=str(tmp_path), pid=os.getpid()
    )
    identity = ProcessIdentity.capture(os.getpid())
    assert owner.process_identity == identity
    session = tmp_path / "session.jsonl"
    session.write_text('{"type":"session","id":"fixture"}\n')
    attached = comms.threads.attach_session(owner.name, str(session))
    assert attached.process_identity == identity
    assert wire(tmp_path).registry.require(owner.name) == attached
    unbound = comms.threads.attach_session(owner.name, str(session), pid=0)
    assert unbound.process_identity is None and unbound.pid == 0
    rebound = comms.threads.attach_session(owner.name, str(session), pid=os.getpid())
    assert rebound.process_identity == identity
    restored = RegistryDocument()
    restored.restore_stopped(comms.registry.snapshot(), (owner.name,))
    saved = restored.threads[owner.name]
    assert saved.process_identity is None and saved.active_turn is None
    assert saved.incarnation == owner.incarnation and saved.session_file == str(session)


def test_same_pid_different_birth_advances_both_authority_domains(tmp_path):
    comms = wire(tmp_path)
    identity = ProcessIdentity.capture(os.getpid())
    owner = Thread("owner", frozenset(), str(tmp_path), process_identity=identity)
    comms.registry.declare(owner)
    before = comms.registry.snapshot()
    stale = replace(identity, start_time=identity.start_time + 1)
    comms.registry.register(replace(owner, process_identity=stale))
    after = comms.registry.snapshot()
    assert after.owner_generations[owner.name] > before.owner_generations[owner.name]
    assert after.admission_generations[owner.name] > before.admission_generations[owner.name]
    assert comms.registry.require(owner.name).pid == os.getpid()
    with pytest.raises(RelationViolationError, match="live owner"):
        comms.registry.live_owner_with_admission(owner.name)
    with pytest.raises(RelationViolationError, match="live owner"):
        comms.registry.lease_local_turn(owner.name, "reused-pid")
    with pytest.raises(RelationViolationError, match="does not own"):
        comms.threads.rename_managed_thread(owner.name, "renamed", owner_pid=os.getpid())
    assert identity.alive()


def test_active_executor_cannot_be_replaced_by_same_pid_different_birth(tmp_path):
    comms = wire(tmp_path)
    identity = ProcessIdentity.capture(os.getpid())
    owner = comms.threads.claim_thread(
        "owner", tags=frozenset(), worktree=str(tmp_path), pid=os.getpid()
    )
    leased, _ = comms.registry.lease_local_turn(owner.name, "active-turn")
    stale = replace(identity, start_time=identity.start_time + 1)
    with pytest.raises(RelationViolationError, match="replace an executor"):
        comms.registry.declare(replace(owner, process_identity=stale))
    assert comms.registry.require(owner.name) == leased
    comms.registry.declare(Thread("owner", frozenset(), str(tmp_path), title="metadata"))
    current = comms.registry.require(owner.name)
    assert current.process_identity == identity and current.active_turn == leased.active_turn
    comms.registry.release_turn(current.turn_lease)
