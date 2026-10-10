"""Actual persisted claim boundaries distinguish declaration from admission drift."""

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
        process_identity=ProcessIdentity.capture(os.getpid()), goal=Goal('task', 'g'),
    ))
    return comms.registry


def test_admission_claim_tolerates_metadata_and_finished_captured_turn(tmp_path):
    registry = registered(tmp_path)
    leased, _ = registry.lease_local_turn('owner', 'first')
    captured, admission = registry.live_owner_with_admission('owner')
    generation = registry.snapshot().owner_generations['owner']
    assert registry.release_turn(leased.turn_lease)[0]
    registry.register(replace(registry.require('owner'), title='metadata', tags=frozenset({'new'})))
    with pytest.raises(RelationViolationError, match='declaration'):
        registry.lease_live_turn_with_generation(
            captured, 'second', expected_owner_generation=generation,
        )
    claimed, current_admission = registry.lease_live_turn_with_admission(
        captured, 'second', expected_generation=admission,
    )
    assert current_admission == admission
    assert claimed.title == 'metadata'
    assert claimed.active_turn.id == 'second'
    assert registry.release_turn(claimed.turn_lease)[0]


@pytest.mark.parametrize('mutation', [
    {'created_at': 200.0}, {'worktree': '/different'}, {'role': ThreadRole.USER},
    {'goal': Goal('replacement', 'other')},
])
def test_raw_registry_writer_cannot_cross_captured_claim_identity(tmp_path, mutation):
    registry = registered(tmp_path)
    captured, admission = registry.live_owner_with_admission('owner')
    # Direct canonical writer, not a fake snapshot or mocked admission.
    with registry.store.editing() as edit:
        edit.document.threads['owner'] = replace(captured, **mutation)
        edit.commit()
    before = registry.store.path.read_bytes()
    with pytest.raises(RelationViolationError):
        registry.lease_live_turn_with_admission(captured, 'denied', expected_generation=admission)
    assert registry.store.path.read_bytes() == before


def test_live_owner_read_rejects_raw_stale_turn_witness(tmp_path):
    registry = registered(tmp_path)
    leased, _ = registry.lease_local_turn('owner', 'turn')
    with registry.store.editing() as edit:
        edit.document.threads['owner'] = replace(
            leased, active_turn=replace(leased.active_turn, admission_generation=99),
        )
        edit.commit()
    with pytest.raises(RelationViolationError, match='turn admission'):
        registry.live_owner_with_admission('owner')
