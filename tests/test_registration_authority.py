"""Actual persisted claim boundaries distinguish declaration from claim_scope drift."""

from agent_comms.repo import Checkout
import os
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.child_process import ProcessIdentity
from test_locked_store import RegistryWithConcurrentDecoder, _hold_lock, event, process
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.nominal.field_codec import TurnId, GoalId
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
    comms.registry.register(Thread('owner', frozenset({'team'}), Checkout(root),
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
        record.writers.replace(replace(record.read(), claimed_generation=RecordedGeneration(99)))
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

    with registry.store.guarded_write(), record.writers.guarded_write():
        waiter = threading.Thread(target=drop_claim)
        waiter.start()
        assert started.wait(5)
        time.sleep(0.2)  # The waiter is parked on this thread's registry writer lock.
        assert registry.release_turn(first.turn_lease)[0]
        second, _ = registry.lease_local_turn('owner', TurnId('16367aacb67a4a017c8da8ab95682ccb'))
        assert not record.path.exists()
    waiter.join(5)
    assert not waiter.is_alive() and failures == []
    assert second.turn_record.read().id == '16367aacb67a4a017c8da8ab95682ccb'


@pytest.mark.parametrize('operation', ['read', 'phase', 'claim', 'sidebar'])
def test_turn_access_waits_for_private_registry_commit(tmp_path, operation):
    from concurrent.futures import ThreadPoolExecutor
    from agent_comms.private_registry_guard import registry_digest
    from agent_comms.store_files import AtomicFile

    registry = registered(tmp_path)
    registry.store.prepare_native_root()
    leased, _ = registry.lease_local_turn('owner', TurnId('a7937b64b8caa58f03721bb6bacf5c78'))
    record, lease = leased.turn_record, leased.require_turn_lease()
    if operation == 'sidebar':
        wire(tmp_path).goals.open_goal_store().create_goal(leased.goal.id)
    entered = threading.Event()

    def access():
        entered.set()
        if operation == 'sidebar':
            return wire(tmp_path).views.presentation.roster('owner')
        if operation == 'read':
            return record.read()
        if operation == 'phase':
            return record.transition(lease, PreparingPhase('after commit'))
        return record.without_claim()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with registry.store.guarded_write():
            guard = registry.store.private_guard_unlocked()
            # Execute the real guard protocol and registry replacement, pausing only
            # at the exact window in which an unlocked reader sees pending state.
            text = registry.store.path.read_text()
            digest = registry_digest(registry.store.path)
            sequence, slot = guard.prepare(digest)
            AtomicFile(registry.store.path).write_text(text)
            future = pool.submit(access)
            try:
                assert entered.wait(5)
                time.sleep(0.1)
                assert not future.done(), 'turn access read an uncommitted registry'
            finally:
                guard.commit(sequence, slot, digest)
        result = future.result(timeout=5)
    if operation == 'sidebar':
        assert 'owner' in result.threads
    elif operation == 'read':
        assert result.id == leased.turn_lease.turn_id
    elif operation == 'phase':
        assert record.read().phase == PreparingPhase('after commit')
    else:
        assert not record.read().has_claim(lease.claimed_generation)


def test_turn_read_still_rejects_abandoned_post_replacement_guard(tmp_path):
    from agent_comms.private_registry_guard import registry_digest
    from agent_comms.store_files import AtomicFile

    registry = registered(tmp_path)
    registry.store.prepare_native_root()
    leased, _ = registry.lease_local_turn('owner', TurnId('a7937b64b8caa58f03721bb6bacf5c78'))
    with registry.store.guarded_write():
        guard = registry.store.private_guard_unlocked()
        text = registry.store.path.read_text()
        guard.prepare(registry_digest(registry.store.path))
        AtomicFile(registry.store.path).write_text(text)
        # Deliberately abandon the real write before committing its authority.
    with pytest.raises(RelationViolationError, match='pending, not committed'):
        leased.turn_record.read()


def test_private_guard_check_survives_coordination_writes(tmp_path, monkeypatch):
    from agent_comms import private_registry_guard
    from agent_comms.coordination_database import CoordinationStore

    registry = registered(tmp_path)
    registry.store.prepare_native_root()
    registry.store.private_guard_unlocked()
    hashed = []
    real = private_registry_guard.registry_digest
    monkeypatch.setattr(private_registry_guard, 'registry_digest',
                        lambda path: hashed.append(path) or real(path))
    coordinator = CoordinationStore.path_in(registry.store.path.parent)
    stat = coordinator.stat()
    os.utime(coordinator, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    registry.store.private_guard_unlocked()
    assert hashed == [], 'a coordination write re-hashed the registry'
    os.utime(registry.store.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    registry.store.private_guard_unlocked()
    assert hashed == [registry.store.path], 'a registry change must re-verify its guard'
