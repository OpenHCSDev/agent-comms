"""Actual persisted claim boundaries distinguish declaration from claim_scope drift."""

import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import TurnId, GoalId
from agent_comms.goals import Goal
from agent_comms.thread_identity import UserThreadRole
from agent_comms.thread_text import GivenThreadText
from agent_comms.turn_lease import RecordedGeneration
from agent_comms.turn_phase import PreparingPhase
from agent_comms.threads import Thread


def registered(root):
    g_goal_id = GoalId.mint()
    comms = wire(root)
    comms.registry.register(Thread('owner', frozenset({'team'}), str(root),
        process_identity=ProcessIdentity.capture(os.getpid()),
        goal=Goal('task', g_goal_id, published_seq=0)))
    return comms.registry


@pytest.mark.parametrize('capture_idle', [False, True])
def test_claim_claim_tolerates_metadata_and_finished_captured_turn(tmp_path, capture_idle):
    registry = registered(tmp_path)
    initial, claim_scope = registry.live_owner_with_claim('owner')
    leased, _ = registry.lease_local_turn('owner', TurnId('a7937b64b8caa58f03721bb6bacf5c78'))
    captured = initial if capture_idle else leased
    generation = registry.snapshot().owner_generations['owner']
    assert registry.release_turn(leased.turn_lease)[0]
    registry.register(replace(registry.require('owner'), title=GivenThreadText('metadata'), tags=frozenset({'new'})))
    with pytest.raises(
        RelationViolationError,
    ):
        registry.lease_live_turn_with_generation(
            captured,
            TurnId('16367aacb67a4a017c8da8ab95682ccb'),
            expected_owner_generation=generation,
        )
    claimed, current_claim = registry.lease_live_turn_with_claim(
        captured, TurnId('16367aacb67a4a017c8da8ab95682ccb'), expected_generation=claim_scope,
    )
    assert current_claim == claim_scope
    assert claimed.title == GivenThreadText('metadata')
    assert claimed.turn_record.read().id == '16367aacb67a4a017c8da8ab95682ccb'
    assert registry.release_turn(claimed.turn_lease)[0]


@pytest.mark.parametrize('mutation', [
    {'created_at': 200.0}, {'worktree': '/different'},
    {'goal': None},
])
def test_raw_registry_writer_cannot_cross_captured_claim_identity(tmp_path, mutation):
    if 'goal' in mutation:
        replacement_goal_id = GoalId.mint()
        mutation = {'goal': Goal('replacement', replacement_goal_id, published_seq=0)}
    registry = registered(tmp_path)
    captured, claim_scope = registry.live_owner_with_claim('owner')
    # Direct canonical writer, not a fake snapshot or mocked claim_scope.
    with registry.store.editing() as edit:
        edit.document.threads['owner'] = replace(captured, **mutation)
        edit.commit()
    before = registry.store.path.read_bytes()
    with pytest.raises(RelationViolationError):
        registry.lease_live_turn_with_claim(
            captured, TurnId('62d6c2330036f64bcf71b95791743d6c'), expected_generation=claim_scope
        )
    assert registry.store.path.read_bytes() == before


def test_a_live_owner_cannot_be_declared_human(tmp_path):
    registry = registered(tmp_path)
    captured, _ = registry.live_owner_with_claim('owner')
    with pytest.raises(RelationViolationError):
        replace(captured, role=UserThreadRole)


def test_live_owner_read_rejects_raw_stale_turn_witness(tmp_path):
    registry = registered(tmp_path)
    leased, _ = registry.lease_local_turn('owner', TurnId('9b473845f684444f80a26586ae298d2b'))
    with registry.store.editing() as edit:
        record = edit.document.threads['owner'].turn_record
        record._publish_unlocked(replace(record.read(), claimed_generation=RecordedGeneration(99)))
        edit.commit()
    with pytest.raises(RelationViolationError):
        registry.live_owner_with_claim('owner')


def test_a_superseded_generation_is_deleted_and_its_late_transition_changes_nothing(tmp_path):
    registry = registered(tmp_path)
    registry.live_owner_with_claim('owner')
    first, _ = registry.lease_local_turn('owner', TurnId('a7937b64b8caa58f03721bb6bacf5c78'))
    stale = first.turn_lease
    assert registry.release_turn(stale)[0]
    second, _ = registry.lease_local_turn('owner', TurnId('16367aacb67a4a017c8da8ab95682ccb'))
    assert not stale.record.path.exists() and second.turn_record.path.exists()
    assert stale.record.transition(stale, PreparingPhase()) == ()
    assert stale.record.observe_native_phase(stale, PreparingPhase()) == ()
    assert second.turn_record.read().id == '16367aacb67a4a017c8da8ab95682ccb'


def test_an_older_snapshot_reads_the_turn_its_thread_names_now(tmp_path):
    registry = registered(tmp_path)
    registry.live_owner_with_claim('owner')
    first, _ = registry.lease_local_turn('owner', TurnId('a7937b64b8caa58f03721bb6bacf5c78'))
    assert registry.release_turn(first.turn_lease)[0]
    second, _ = registry.lease_local_turn('owner', TurnId('16367aacb67a4a017c8da8ab95682ccb'))
    assert not first.turn_record.path.exists()
    assert first.turn_state.active.id == '16367aacb67a4a017c8da8ab95682ccb'
    assert first.turn_record.read() == second.turn_record.read()
