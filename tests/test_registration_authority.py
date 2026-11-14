"""Actual persisted claim boundaries distinguish declaration from claim_scope drift."""

import os
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.turn_lease import RecordedGeneration
from agent_comms.execution.turn_phase import PreparingPhase
from agent_comms.nominal.field_codec import GoalId, TurnId
from agent_comms.planning.goals import Goal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import UserThreadRole
from agent_comms.registry.thread_text import GivenThreadText
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import DocumentChange
from agent_comms.storage.store_files import SharedLock
from test_locked_store import RegistryWithConcurrentDecoder, _hold_lock, event, process


def registered(root):
    g_goal_id = GoalId.mint()
    comms = wire(root)
    comms.registry.register(NativeThread('owner', frozenset({'team'}), Checkout(root),
        process_identity=ProcessIdentity.capture(os.getpid()),
        goal=Goal('task', g_goal_id, published_seq=0)))
    return comms.registry


@pytest.mark.parametrize('capture_idle', [False, True])
def test_claim_claim_tolerates_metadata_and_finished_captured_turn(tmp_path, capture_idle):
    registry = registered(tmp_path)
    initial, claim_scope = registry.live_owner_with_claim('owner')
    leased, _ = registry.lease_local_turn('owner', TurnId('a7937b64b8caa58f03721bb6bacf5c78'))
    captured = initial if capture_idle else leased
    assert registry.release_turn(leased.turn_lease)[0]
    registry.register(replace(registry.require('owner'), title=GivenThreadText('metadata'), tags=frozenset({'new'})))
    claimed, current_claim = registry.lease_live_turn_with_claim(
        captured, TurnId('16367aacb67a4a017c8da8ab95682ccb'), expected_generation=claim_scope,
    )
    assert current_claim == claim_scope
    assert claimed.title == GivenThreadText('metadata')
    assert claimed.turn_record.read().id == '16367aacb67a4a017c8da8ab95682ccb'
    assert registry.release_turn(claimed.turn_lease)[0]


@pytest.mark.parametrize('mutation', [
    {'created_at': 200.0}, {'worktree': Checkout(Path('/different'))},
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
        record.writers.update(lambda active: DocumentChange.to(
            replace(active, claimed_generation=RecordedGeneration(99))))
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
    rows = registry.store.rows.turn_wires((stale.record.identity, second.turn_record.identity))
    assert stale.record.identity not in rows and second.turn_record.identity in rows
    assert stale.record.transition(stale, PreparingPhase()).states == ()
    assert stale.record.observe_native_phase(stale, PreparingPhase()).states == ()
    assert second.turn_record.read().id == '16367aacb67a4a017c8da8ab95682ccb'


def test_an_older_snapshot_reads_the_turn_its_thread_names_now(tmp_path):
    registry = registered(tmp_path)
    registry.live_owner_with_claim('owner')
    first, _ = registry.lease_local_turn('owner', TurnId('a7937b64b8caa58f03721bb6bacf5c78'))
    assert registry.release_turn(first.turn_lease)[0]
    second, _ = registry.lease_local_turn('owner', TurnId('16367aacb67a4a017c8da8ab95682ccb'))
    assert not registry.store.rows.turn_wires((first.turn_record.identity,))
    assert first.turn_state.active.id == '16367aacb67a4a017c8da8ab95682ccb'
    assert first.turn_record.read() == second.turn_record.read()


def test_turn_capture_releases_registry_writers_before_decoding(tmp_path):
    registry = registered(tmp_path)
    leased, _ = registry.lease_local_turn('owner', TurnId('a7937b64b8caa58f03721bb6bacf5c78'))
    reader = RegistryWithConcurrentDecoder(registry.store.path)
    captured = reader.read_turn(leased.turn_record)
    assert captured.id == leased.turn_record.read().id


def test_a_phase_change_completes_while_another_process_reads_the_registry(tmp_path):
    registry = registered(tmp_path)
    registry.live_owner_with_claim('owner')
    leased, _ = registry.lease_local_turn('owner', TurnId('a7937b64b8caa58f03721bb6bacf5c78'))
    lease = leased.require_turn_lease()
    ready, release = event(), event()
    # A phase change that waited on the registry would hit the store lock's own wait limit
    # (StoreLockContention, store_files.py) and fail with StoreLockBusyError.
    with process(_hold_lock, registry.store.path, SharedLock(), ready, release):
        try:
            assert ready.wait(5)
            effects = lease.record.transition(lease, PreparingPhase('reading'))
            assert [state.active.phase for state in effects.states] == [PreparingPhase('reading')]
            assert lease.record.read().phase == PreparingPhase('reading')
        finally:
            release.set()



def test_a_claim_drop_after_its_turn_retired_changes_nothing(tmp_path):
    """A claim drop writes its own turn row and takes no registry lock: once a registry commit
    retired the turn its row is gone, so the drop finds nothing and the next turn is untouched."""
    registry = registered(tmp_path)
    registry.live_owner_with_claim('owner')
    first, _ = registry.lease_local_turn('owner', TurnId('a7937b64b8caa58f03721bb6bacf5c78'))
    record = first.turn_record
    assert registry.release_turn(first.turn_lease)[0]
    second, _ = registry.lease_local_turn('owner', TurnId('16367aacb67a4a017c8da8ab95682ccb'))
    assert not registry.store.rows.turn_wires((record.identity,))
    record.without_claim()
    assert second.turn_record.read().id == '16367aacb67a4a017c8da8ab95682ccb'
    assert second.turn_lease.is_leased()


def test_a_claim_dropped_before_its_turn_retires_leaves_the_release_to_the_lease(tmp_path):
    """Dropped first, the claim is gone from the row; the lease taken before the drop still
    releases its turn, and the next lease supersedes it and deletes the row."""
    registry = registered(tmp_path)
    registry.live_owner_with_claim('owner')
    first, _ = registry.lease_local_turn('owner', TurnId('a7937b64b8caa58f03721bb6bacf5c78'))
    lease = first.turn_lease
    first.turn_record.without_claim()
    assert registry.release_turn(lease)[0]
    registry.lease_local_turn('owner', TurnId('16367aacb67a4a017c8da8ab95682ccb'))
    assert not registry.store.rows.turn_wires((first.turn_record.identity,))


