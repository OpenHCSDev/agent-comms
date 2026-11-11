"""Failure evidence is passive; only its fenced attempt owns terminal writes."""
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.errors import StaleAttemptError, UnresolvedAttemptError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.turn_failure import BackendFailed, FailedTurnResolution, FinalStopMissing
from agent_comms.execution.turn_lease import FinishedTurn, StoredTurnRecord, TurnLeaseFence
from agent_comms.nominal.field_codec import GoalId, TurnId
from agent_comms.planning.goal_attempts import GoalAttemptStore
from agent_comms.planning.goal_failure_observation import NoFailedTurnEvidence
from agent_comms.planning.goal_generation import BlockedGeneration
from agent_comms.planning.goal_ledger import AttemptRecord, FailedTurnObservation, Generation
from agent_comms.planning.goals import Goal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.registry.thread_identity import RecordedThreadIncarnation, TurnIdentity
from agent_comms.registry.threads import NativeThread


@pytest.fixture
def bound(tmp_path, *, thread_registration):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = GoalAttemptStore.initialize(root)
    goal_id = GoalId.mint()
    goal = Goal("private goal prose", goal_id, published_seq=0, revision=2)
    owner = NativeThread(
        'owner', frozenset(), Checkout(Path('/private-worktree')),
        process_identity=ProcessIdentity.capture(os.getpid()), created_at=10.0,
        goal=goal, turn_generation=7, turn_record=FinishedTurn(TurnId('a' * 32)))
    store.create_goal(goal.id)
    permit = store.claim_launch(store.reserve(goal.id, 1))
    identity = TurnIdentity(owner.incarnation, 7)
    lease = TurnLeaseFence(
        identity, TurnId('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'), 3,
        record=StoredTurnRecord.for_identity(thread_registration.store.path, identity, registry_type=RegistryStore),
    )
    observation = FailedTurnObservation.from_terminal(
        permit.reservation, owner=owner, goal=goal, lease=lease,
        turn_id=lease.turn_id, claim_scope=3, current_owner=owner, current_claim=3,
        failure=FinalStopMissing("Pi ended without its final stop"),
    )
    assert observation is not None
    return store, owner, lease, observation


def rows(store, table):
    with sqlite3.connect(store.path) as conn:
        return conn.execute(f"SELECT * FROM {table}").fetchall()


def test_duplicate_callback_and_restart_preserve_failure_bytes(bound):
    store, owner, _, observation = bound
    observation.reservation.fail(
        store, FailedTurnResolution(BackendFailed), observation=observation
    )
    before = store.path.read_bytes()
    for current in (store, GoalAttemptStore.at(store.root)):
        with pytest.raises(StaleAttemptError):
            observation.reservation.fail(
                current, FailedTurnResolution(BackendFailed), observation=observation
            )
        with pytest.raises(UnresolvedAttemptError):
            current.resume(owner.goal.id, 1)
        with current.reader_snapshot() as connection:
            assert Generation.require(connection, goal_id=owner.goal.id).failure_evidence(connection) == observation.evidence
    assert store.path.read_bytes() == before
    assert len(rows(store, "failed_turn_evidence")) == 1
    assert observation.reservation.token not in json.dumps(rows(store, "failed_turn_evidence"))


@pytest.mark.parametrize("mutation", [
    {"identity": TurnIdentity(RecordedThreadIncarnation("owner", 11.0), 7)},
    {"turn_id": TurnId.mint()}, {"claimed_generation": 4},
    {"identity": TurnIdentity(RecordedThreadIncarnation("owner", 10.0), 8)},
])
def test_mismatched_turn_lease_is_not_bound(bound, mutation):
    store, owner, lease, observation = bound
    rejected = FailedTurnObservation.from_terminal(
        observation.reservation, owner=owner, goal=owner.goal,
        lease=replace(lease, **mutation), turn_id=lease.turn_id, claim_scope=3,
        current_owner=owner, current_claim=3, failure=BackendFailed("the provider refused the request"),
    )
    assert rejected is None
    observation.reservation.fail(store, FailedTurnResolution(BackendFailed), observation=rejected)
    assert store.snapshot(owner.goal.id).lifecycle == BlockedGeneration()
    with store.reader_snapshot() as connection:
        assert Generation.require(connection, goal_id=owner.goal.id).failure_evidence(connection) == NoFailedTurnEvidence()


@pytest.mark.parametrize("binding", ["token", "evidence_identity"])
def test_mismatched_permit_cannot_attach_observation_or_weaken_failure(bound, binding):
    store, owner, _, observation = bound
    another_goal_id = GoalId.mint()
    forged = (
        replace(observation, reservation=replace(observation.reservation, token="wrong"))
        if binding == "token"
        else replace(observation, evidence=replace(observation.evidence, goal_id=another_goal_id))
    )
    observation.reservation.fail(store, FailedTurnResolution(BackendFailed), observation=forged)
    assert rows(store, "failed_turn_evidence") == []
    assert store.snapshot(owner.goal.id).lifecycle == BlockedGeneration()
    with store.reader_snapshot() as connection:
        assert Generation.require(connection, goal_id=owner.goal.id).failure_evidence(connection) == NoFailedTurnEvidence()


def test_unclaimed_attempt_failure_has_no_backend_incident(bound):
    unclaimed_goal_id = GoalId.mint()
    store, owner, _, observation = bound
    store.create_goal(unclaimed_goal_id)
    reservation = store.reserve(unclaimed_goal_id, 1)
    reservation.fail(
        store,
        FailedTurnResolution(BackendFailed),
        observation=replace(observation, reservation=reservation),
    )
    assert store.snapshot(unclaimed_goal_id).lifecycle == BlockedGeneration()
    assert rows(store, "failed_turn_evidence") == []


def test_stale_attempt_cannot_record_an_incident(bound):
    store, owner, _, observation = bound
    store.retire_goal(owner.goal.id, expected_generation=1,
        attempt_id=observation.reservation.attempt_id)
    before = store.path.read_bytes()
    with pytest.raises(StaleAttemptError):
        observation.reservation.fail(
            store, FailedTurnResolution(BackendFailed), observation=observation
        )
    assert store.path.read_bytes() == before
    assert rows(store, "failed_turn_evidence") == []


def test_observation_insert_error_does_not_rollback_failure_fence(bound):
    store, owner, _, observation = bound
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "CREATE TRIGGER reject_observation BEFORE INSERT ON failed_turn_evidence "
            "BEGIN SELECT RAISE(ABORT,'injected observation failure'); END"
        )
    observation.reservation.fail(
        store, FailedTurnResolution(BackendFailed), observation=observation
    )
    assert store.snapshot(owner.goal.id).lifecycle == BlockedGeneration()
    with sqlite3.connect(store.path) as conn:
        assert AttemptRecord.one(conn, attempt_id=observation.reservation.attempt_id).phase.declared_name == "failed"
    assert rows(store, "failed_turn_evidence") == []
    with pytest.raises(UnresolvedAttemptError):
        store.resume(owner.goal.id, 1)
