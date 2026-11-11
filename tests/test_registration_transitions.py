"""Transition-family consequences on the actual canonical registry and locks."""

import os
from dataclasses import replace

import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.session_file import RegistrySessionLocation
from agent_comms.execution.turn_lease import (
    ActiveTurn,
    NoFinishedTurn,
)
from agent_comms.nominal.field_codec import TurnId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registration import Registration
from agent_comms.registry.thread_identity import (
    ReportedTools,
    ToolName,
    UnreportedTools,
)
from agent_comms.registry.thread_status import StoppedThreadStatus
from agent_comms.registry.thread_text import GivenThreadText
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import RegistryFile


def _metadata(registry, leased, lease, tmp_path):
    return replace(leased, title=GivenThreadText('new title'), tags=frozenset({'changed'})), {}


def _session(registry, leased, lease, tmp_path):
    return replace(leased, session_file=RegistrySessionLocation.from_path(str(tmp_path / 'new.jsonl'), tmp_path)), {}


def _restart(registry, leased, lease, tmp_path):
    return leased, {'new_owner': True}


def _stop(registry, leased, lease, tmp_path):
    return leased, {'status': StoppedThreadStatus()}


def _new_turn(registry, leased, lease, tmp_path):
    assert registry.release_turn(lease)[0]
    return registry.lease_local_turn('owner', TurnId('5f54227b74fbba7743c47cd286b4873f'))[0], {}


@pytest.mark.parametrize('transition, advances_owner, preserves_record', [
    (_metadata, False, True),
    (_session, False, True),
    (_restart, True, True),
    (_stop, True, True),
    (_new_turn, False, False),
])
def test_registration_transition_preserves_or_revokes_exact_lease(
    tmp_path, transition, advances_owner, preserves_record,
):
    registry = Registration(RegistryFile.path_in(tmp_path))
    thread = NativeThread('owner', frozenset({'team'}), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    registry.register(thread)
    leased, _ = registry.lease_local_turn('owner', TurnId('9b473845f684444f80a26586ae298d2b'))
    lease = leased.require_turn_lease()
    before = registry.snapshot()
    updated, kwargs = transition(registry, leased, lease, tmp_path)
    registry.register(updated, **kwargs)
    after = registry.snapshot()
    assert (after.owner_generations['owner'] > before.owner_generations['owner']) == advances_owner
    assert (after.claim_generation('owner') > before.claim_generation('owner')) == advances_owner
    assert (after.threads['owner'].turn_record == lease.record) == preserves_record
    assert (after.threads['owner'].turn_lease == lease) == (preserves_record and not advances_owner)
    # The original lease may retire a revoked record, but never attest completion
    # after restart/stop revoked its input claim.
    released, completion = registry.release_turn(lease)
    assert released == preserves_record
    assert (completion != NoFinishedTurn()) == (preserves_record and not advances_owner)


def tools(version, *names):
    return ReportedTools(version, frozenset(ToolName(name) for name in names))


@pytest.mark.parametrize('restart, before, after, lost', [
    ('new-owner', tools('1', 'a', 'b'), tools('1', 'a'), ('missing tools b; runtime 1',)),
    ('stop-start', tools('1', 'a', 'b'), tools('1', 'a'), ('missing tools b; runtime 1',)),
    ('stop-stop-start', tools('1', 'a', 'b'), tools('1', 'a'), ('missing tools b; runtime 1',)),
    ('stop-start', tools('1', 'a', 'b'), tools('1', 'a', 'b'), ()),
    ('new-owner', UnreportedTools('1'), UnreportedTools('1'), ()),
    ('new-owner', UnreportedTools('1'), UnreportedTools('2'), ('runtime 1 -> 2, tool set unreported',)),
])
def test_owner_restart_compares_tools_with_last_real_record(tmp_path, restart, before, after, lost):
    registry = Registration(RegistryFile.path_in(tmp_path))
    registry.register(NativeThread('owner', frozenset(), Checkout(tmp_path)))
    registry.observe_offered_tools(registry.require('owner').incarnation, before)
    stops = restart.count('stop')
    for _ in range(stops):
        registry.register(registry.require('owner'), status=StoppedThreadStatus())
    registry.register(registry.require('owner'), new_owner=True)
    current = registry.require('owner')
    assert current.offered_before_restart == before
    registry.observe_offered_tools(current.incarnation, after)
    assert registry.require('owner').lost_tools() == lost


def test_rename_preserves_exact_turn_release_but_not_reused_identity(tmp_path):
    registry = Registration(RegistryFile.path_in(tmp_path))
    thread = NativeThread('owner', frozenset(), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    registry.register(thread)
    leased, _ = registry.lease_local_turn('owner', TurnId('058be655fae2a4c37a21d2c091ba7661'))
    lease = leased.require_turn_lease()
    registry.rename('owner', 'renamed')
    released, finished = registry.release_turn(lease)
    assert released and finished.identity.incarnation.name == 'renamed'
    next_turn, _ = registry.lease_local_turn('renamed', TurnId('058be655fae2a4c37a21d2c091ba7661'))
    assert registry.release_turn(lease) == (False, NoFinishedTurn())
    assert registry.require('renamed').turn_lease == next_turn.turn_lease


def test_idle_restart_fence_cannot_borrow_metadata_or_claim(tmp_path):
    registry = Registration(RegistryFile.path_in(tmp_path))
    registry.register(NativeThread('owner', frozenset(), Checkout(tmp_path)))
    original = registry.snapshot()
    expected = original.threads['owner']
    claim_scope = original.claim_generation('owner')
    registry.register(replace(expected, title=GivenThreadText('changed')))
    with pytest.raises(RelationViolationError):
        with registry.store.editing() as edit:
            edit.fence_restart_owners(((expected, claim_scope),))
    current = registry.require('owner')
    registry.register(current, new_owner=True)
    with pytest.raises(RelationViolationError):
        with registry.store.editing() as edit:
            edit.fence_restart_owners(((current, claim_scope),))
    generation = registry.snapshot().claim_generation('owner')
    with registry.store.editing() as edit:
        assert edit.fence_restart_owners(((current, generation),))[0][1] > generation
    assert registry.status('owner').is_stopped()


def test_batch_idle_fence_refuses_busy_second_without_partial_commit(tmp_path):
    registry = Registration(RegistryFile.path_in(tmp_path))
    for name in ("first", "second"):
        registry.register(NativeThread(name, frozenset(), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    before = registry.snapshot()
    selected = tuple((before.threads[name], before.claim_generation(name)) for name in ("first", "second"))
    registry.lease_local_turn('second', TurnId('c9bc072f4fa8189466c2a8f2c36a56a4'))
    protected = registry.snapshot()
    with pytest.raises(RelationViolationError):
        with registry.store.editing() as edit:
            edit.fence_restart_owners(selected)
    assert registry.snapshot() == protected


def test_restart_fence_ends_a_wedged_turn_the_way_stop_does(tmp_path, monkeypatch):
    registry = Registration(RegistryFile.path_in(tmp_path))
    registry.register(NativeThread("owner", frozenset(), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    registry.lease_local_turn("owner", TurnId("c9bc072f4fa8189466c2a8f2c36a56a4"))
    busy = registry.snapshot()
    selected = ((busy.threads["owner"], busy.claim_generation("owner")),)
    with pytest.raises(RelationViolationError):
        with registry.store.editing() as edit:
            edit.fence_restart_owners(selected)  # a turn that just wrote is working, not wedged
    monkeypatch.setattr(ActiveTurn, "wedged_after", 0.0)  # its transcript has been silent past the limit
    with registry.store.editing() as edit:
        edit.fence_restart_owners(selected)
    assert registry.status("owner").is_stopped()
    assert not registry.require("owner").executing
