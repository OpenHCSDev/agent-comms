"""Actual persisted claim boundaries distinguish declaration from claim_scope drift."""

import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.goals import Goal
from agent_comms.thread_identity import UserThreadRole
from agent_comms.thread_text import GivenThreadText
from agent_comms.turn_lease import RecordedGeneration


def registered(root):
    comms = wire(root)
    comms.registry.register(comms.registry.new_thread('owner', frozenset({'team'}), str(root), process_identity=ProcessIdentity.capture(os.getpid()), goal=Goal('task', 'g', published_seq=0)))
    return comms.registry


@pytest.mark.parametrize('capture_idle', [False, True])
def test_claim_claim_tolerates_metadata_and_finished_captured_turn(tmp_path, capture_idle):
    registry = registered(tmp_path)
    initial, claim_scope = registry.live_owner_with_claim('owner')
    leased, _ = registry.lease_local_turn('owner', 'first')
    captured = initial if capture_idle else leased
    generation = registry.snapshot().owner_generations['owner']
    assert registry.release_turn(leased.turn_lease)[0]
    registry.register(replace(registry.require('owner'), title=GivenThreadText('metadata'), tags=frozenset({'new'})))
    with pytest.raises(
        RelationViolationError,
    ):
        registry.lease_live_turn_with_generation(
            captured, 'second', expected_owner_generation=generation,
        )
    claimed, current_claim = registry.lease_live_turn_with_claim(
        captured, 'second', expected_generation=claim_scope,
    )
    assert current_claim == claim_scope
    assert claimed.title == GivenThreadText('metadata')
    assert claimed.turn_record.read().id == 'second'
    assert registry.release_turn(claimed.turn_lease)[0]


@pytest.mark.parametrize('mutation', [
    {'created_at': 200.0}, {'worktree': '/different'},
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


def test_a_live_owner_cannot_be_declared_human(tmp_path):
    registry = registered(tmp_path)
    captured, _ = registry.live_owner_with_claim('owner')
    with pytest.raises(RelationViolationError):
        replace(captured, role=UserThreadRole)


def test_live_owner_read_rejects_raw_stale_turn_witness(tmp_path):
    registry = registered(tmp_path)
    leased, _ = registry.lease_local_turn('owner', 'turn')
    with registry.store.editing() as edit:
        record = edit.document.threads['owner'].turn_record
        record._publish_unlocked(replace(record.read(), claimed_generation=RecordedGeneration(99)))
        edit.commit()
    with pytest.raises(RelationViolationError):
        registry.live_owner_with_claim('owner')
