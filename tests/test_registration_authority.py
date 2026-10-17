"""Actual persisted claim boundaries distinguish declaration from claim_scope drift."""
from agent_comms.storage_rules import StorageRule

import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.goals import Goal
from agent_comms.thread_identity import ThreadRole
from agent_comms.threads import Thread


def registered(root):
    comms = wire(root)
    comms.registry.register(Thread(
        'owner', frozenset({'team'}), str(root),
        process_identity=ProcessIdentity.capture(os.getpid()), goal=Goal('task', 'g', published_seq=0),
    storage_roots=StorageRule().initial_roots('owner', str(root))))
    return comms.registry


def test_claim_claim_tolerates_metadata_and_finished_captured_turn(tmp_path):
    registry = registered(tmp_path)
    leased, _ = registry.lease_local_turn('owner', 'first')
    captured, claim_scope = registry.live_owner_with_claim('owner')
    generation = registry.snapshot().owner_generations['owner']
    assert registry.release_turn(leased.turn_lease)[0]
    registry.register(replace(registry.require('owner'), title='metadata', tags=frozenset({'new'})))
    with pytest.raises(RelationViolationError, match='declaration'):
        registry.lease_live_turn_with_generation(
            captured, 'second', expected_owner_generation=generation,
        )
    claimed, current_claim = registry.lease_live_turn_with_claim(
        captured, 'second', expected_generation=claim_scope,
    )
    assert current_claim == claim_scope
    assert claimed.title == 'metadata'
    assert claimed.active_turn.id == 'second'
    assert registry.release_turn(claimed.turn_lease)[0]


@pytest.mark.parametrize('mutation', [
    {'created_at': 200.0}, {'worktree': '/different'}, {'role': ThreadRole.USER},
    {'goal': Goal('replacement', 'other', published_seq=0)},
])
def test_raw_registry_writer_cannot_cross_captured_claim_identity(tmp_path, mutation):
    registry = registered(tmp_path)
    captured, claim_scope = registry.live_owner_with_claim('owner')
    # Direct canonical writer, not a fake snapshot or mocked claim_scope.
    with registry.store.editing() as edit:
        edit.document.threads['owner'] = replace(captured, **mutation)
        edit.commit()
    before = registry.store.path.read_bytes()
    with pytest.raises(RelationViolationError):
        registry.lease_live_turn_with_claim(captured, 'denied', expected_generation=claim_scope)
    assert registry.store.path.read_bytes() == before


def test_live_owner_read_rejects_raw_stale_turn_witness(tmp_path):
    registry = registered(tmp_path)
    leased, _ = registry.lease_local_turn('owner', 'turn')
    with registry.store.editing() as edit:
        edit.document.threads['owner'] = replace(
            leased, active_turn=replace(leased.active_turn, claimed_generation=99),
        )
        edit.commit()
    with pytest.raises(RelationViolationError, match='turn claim_scope'):
        registry.live_owner_with_claim('owner')
