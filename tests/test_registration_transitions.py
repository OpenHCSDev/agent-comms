"""Transition-family consequences on the actual canonical registry and locks."""

import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.registration import Registration
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.threads import Thread
from agent_comms.turn_lease import ActiveTurn


@pytest.mark.parametrize('transition', ['metadata', 'session', 'restart', 'stop', 'new-turn'])
def test_registration_transition_preserves_or_revokes_exact_lease(tmp_path, transition):
    registry = Registration(tmp_path / 'registry.json')
    thread = Thread('owner', frozenset({'team'}), str(tmp_path),
                    process_identity=ProcessIdentity.capture(os.getpid()))
    registry.register(thread)
    leased, _ = registry.lease_local_turn('owner', 'turn')
    before = registry.snapshot()
    updated = leased
    kwargs = {}
    advances_owner = False
    preserves_lease = True
    if transition == 'metadata':
        updated = replace(leased, title='new title', tags=frozenset({'changed'}))
    elif transition == 'session':
        updated = replace(leased, session_file=str(tmp_path / 'new.jsonl'))
    elif transition == 'restart':
        kwargs['new_owner'] = True
        advances_owner, preserves_lease = True, False
    elif transition == 'stop':
        kwargs['status'] = StoppedThreadStatus()
        advances_owner = True
    elif transition == 'new-turn':
        updated = replace(leased, active_turn=ActiveTurn('imported', leased.pid))
        preserves_lease = False
    registry.register(updated, **kwargs)
    after = registry.snapshot()
    assert (after.owner_generations['owner'] > before.owner_generations['owner']) == advances_owner
    assert (after.admission_generations['owner'] > before.admission_generations['owner']) == advances_owner
    assert (after.threads['owner'].turn_lease == leased.turn_lease) == preserves_lease
    released, completion = registry.release_turn(leased.turn_lease)
    assert released == preserves_lease
    assert (completion is not None) == (preserves_lease and not advances_owner)


def test_rename_preserves_exact_turn_release_but_not_reused_identity(tmp_path):
    registry = Registration(tmp_path / 'registry.json')
    thread = Thread('owner', frozenset(), str(tmp_path),
                    process_identity=ProcessIdentity.capture(os.getpid()))
    registry.register(thread)
    leased, _ = registry.lease_local_turn('owner', 'same-id')
    registry.rename('owner', 'renamed')
    released, finished = registry.release_turn(leased.turn_lease)
    assert released and finished.identity.incarnation.name == 'renamed'
    next_turn, _ = registry.lease_local_turn('renamed', 'same-id')
    assert registry.release_turn(leased.turn_lease) == (False, None)
    assert registry.require('renamed').turn_lease == next_turn.turn_lease


def test_idle_restart_fence_cannot_borrow_metadata_or_admission(tmp_path):
    registry = Registration(tmp_path / 'registry.json')
    registry.register(Thread('owner', frozenset(), str(tmp_path)))
    original = registry.snapshot()
    expected = original.threads['owner']
    admission = original.admission_generations['owner']
    registry.register(replace(expected, title='changed'))
    with pytest.raises(RelationViolationError, match='changed'):
        registry.fence_idle_owner(expected, expected_admission_generation=admission)
    current = registry.require('owner')
    registry.register(current, new_owner=True)
    with pytest.raises(RelationViolationError, match='admission'):
        registry.fence_idle_owner(current, expected_admission_generation=admission)
    generation = registry.snapshot().admission_generations['owner']
    assert registry.fence_idle_owner(current, expected_admission_generation=generation) > generation
    assert registry.status('owner').stopped
