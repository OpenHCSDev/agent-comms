"""Transition-family consequences on the actual canonical registry and locks."""
from agent_comms.field_codec import TurnId

import os
from dataclasses import replace

import pytest

from agent_comms.session_file import RegistrySessionLocation
from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.registration import Registration
from agent_comms.thread_identity import ReportedTools, ToolName, UnreportedTools
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.turn_lease import NoFinishedTurn
from agent_comms.thread_text import GivenThreadText
from agent_comms.threads import Thread


@pytest.mark.parametrize('transition', ['metadata', 'session', 'restart', 'stop', 'new-turn'])
def test_registration_transition_preserves_or_revokes_exact_lease(tmp_path, transition):
    registry = Registration(tmp_path / 'registry.json')
    thread = Thread('owner', frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    registry.register(thread)
    leased, _ = registry.lease_local_turn('owner', TurnId('9b473845f684444f80a26586ae298d2b'))
    lease = leased.require_turn_lease()
    before = registry.snapshot()
    updated = leased
    kwargs = {}
    advances_owner = False
    preserves_record = True
    if transition == 'metadata':
        updated = replace(leased, title=GivenThreadText('new title'), tags=frozenset({'changed'}))
    elif transition == 'session':
        updated = replace(leased, session_file=RegistrySessionLocation.from_path(str(tmp_path / 'new.jsonl'), tmp_path))
    elif transition == 'restart':
        kwargs['new_owner'] = True
        advances_owner = True
    elif transition == 'stop':
        kwargs['status'] = StoppedThreadStatus()
        advances_owner = True
    elif transition == 'new-turn':
        assert registry.release_turn(lease)[0]
        updated, _ = registry.lease_local_turn('owner', TurnId('5f54227b74fbba7743c47cd286b4873f'))
        preserves_record = False
    registry.register(updated, **kwargs)
    after = registry.snapshot()
    assert (after.owner_generations['owner'] > before.owner_generations['owner']) == advances_owner
    assert (after.claim_generation('owner') > before.claim_generation('owner')) == advances_owner
    assert (after.threads['owner'].turn_record == lease.record) == preserves_record
    assert (after.threads['owner'].turn_lease == lease) == (preserves_record and not advances_owner)
    # P482 (1b9feceea) lets the original lease retire a revoked record, but
    # never attest completion after restart/stop revoked its input claim.
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
    registry = Registration(tmp_path / 'registry.json')
    registry.register(Thread('owner', frozenset(), str(tmp_path)))
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
    registry = Registration(tmp_path / 'registry.json')
    thread = Thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
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
    registry = Registration(tmp_path / 'registry.json')
    registry.register(Thread('owner', frozenset(), str(tmp_path)))
    original = registry.snapshot()
    expected = original.threads['owner']
    claim_scope = original.claim_generation('owner')
    registry.register(replace(expected, title=GivenThreadText('changed')))
    with pytest.raises(RelationViolationError):
        registry.fence_idle_owners(((expected, claim_scope),))
    current = registry.require('owner')
    registry.register(current, new_owner=True)
    with pytest.raises(RelationViolationError):
        registry.fence_idle_owners(((current, claim_scope),))
    generation = registry.snapshot().claim_generation('owner')
    assert registry.fence_idle_owners(((current, generation),))[0][1] > generation
    assert registry.status('owner').stopped


def test_batch_idle_fence_refuses_busy_second_without_partial_commit(tmp_path):
    registry = Registration(tmp_path / "registry.json")
    for name in ("first", "second"):
        registry.register(Thread(name, frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    before = registry.snapshot()
    selected = tuple((before.threads[name], before.claim_generation(name)) for name in ("first", "second"))
    registry.lease_local_turn('second', TurnId('c9bc072f4fa8189466c2a8f2c36a56a4'))
    protected = registry.snapshot()
    with pytest.raises(RelationViolationError):
        registry.fence_idle_owners(selected)
    assert registry.snapshot() == protected
