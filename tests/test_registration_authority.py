"""Actual persisted claim boundaries distinguish declaration from claim_scope drift."""

import os
import threading
import time
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from test_locked_store import _hold_lock, event, process
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import TurnId, GoalId
from agent_comms.goals import Goal
from agent_comms.store_files import SharedLock, store_lock_path
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
    assert not store_lock_path(stale.record.path).exists()
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


def test_a_phase_change_completes_while_another_process_reads_the_registry(tmp_path):
    registry = registered(tmp_path)
    registry.live_owner_with_claim('owner')
    leased, _ = registry.lease_local_turn('owner', TurnId('a7937b64b8caa58f03721bb6bacf5c78'))
    lease = leased.require_turn_lease()
    ready, release = event(), event()
    # A phase change still waiting on the registry outlives the holder's bound, which fails it.
    with process(_hold_lock, registry.store.path, SharedLock(), ready, release):
        try:
            assert ready.wait(5)
            effects = lease.record.transition(lease, PreparingPhase('reading'))
            assert [state.active.phase for state in effects] == [PreparingPhase('reading')]
            assert lease.record.read().phase == PreparingPhase('reading')
        finally:
            release.set()



def test_a_claim_drop_waiting_on_a_turn_retired_under_it_finds_the_retirement(tmp_path):
    registry = registered(tmp_path)
    registry.live_owner_with_claim('owner')
    first, _ = registry.lease_local_turn('owner', TurnId('a7937b64b8caa58f03721bb6bacf5c78'))
    record = first.turn_record
    started, failures = threading.Event(), []

    def drop_claim():
        started.set()
        try:
            record.without_claim()
        except Exception as error:
            failures.append(error)

    with record.writers.locked():
        waiter = threading.Thread(target=drop_claim)
        waiter.start()
        assert started.wait(5)
        time.sleep(0.2)  # The waiter is parked on the writer lock this thread holds.
        assert registry.release_turn(first.turn_lease)[0]
        second, _ = registry.lease_local_turn('owner', TurnId('16367aacb67a4a017c8da8ab95682ccb'))
        assert not record.path.exists()
    waiter.join(5)
    assert not waiter.is_alive() and failures == []
    assert second.turn_record.read().id == '16367aacb67a4a017c8da8ab95682ccb'
