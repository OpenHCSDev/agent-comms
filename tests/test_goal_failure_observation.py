"""Failure evidence is passive; only its fenced attempt owns terminal writes."""

import json
import os
import sqlite3
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.diagnostics import FailureReason
from agent_comms.goal_attempts import (
    AttemptRecord,
    GoalAttemptStore,
    StaleAttemptError,
    UnresolvedAttemptError,
)
from agent_comms.goal_failure_observation import FailedTurnObservation, NoFailedTurnEvidence
from agent_comms.goal_generation import BlockedGeneration
from agent_comms.goals import Goal
from agent_comms.thread_identity import RecordedThreadIncarnation, TurnIdentity
from agent_comms.turn_lease import FinishedTurn, StoredTurnRecord, TurnLeaseFence


@pytest.fixture
def bound(tmp_path, *, thread_registration):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = GoalAttemptStore.initialize(root)
    goal = Goal("private goal prose", "goal", published_seq=0, revision=2)
    owner = thread_registration.new_thread(
        'owner', frozenset(), '/private-worktree',
        process_identity=ProcessIdentity.capture(os.getpid()), created_at=10.0,
        goal=goal, turn_generation=7, turn_record=FinishedTurn('a' * 32),
    )
    store.create_goal(goal.id)
    permit = store.claim_launch(store.reserve(goal.id, 1))
    identity = TurnIdentity(owner.incarnation, 7)
    lease = TurnLeaseFence(
        identity, "a" * 32, 3,
        record=StoredTurnRecord.for_identity(thread_registration.store.path, identity),
    )
    observation = FailedTurnObservation.from_terminal(
        permit.reservation, owner=owner, goal=goal, lease=lease,
        turn_id=lease.turn_id, claim_scope=3, current_owner=owner, current_claim=3,
        reason=FailureReason.FINAL_STOP_MISSING,
    )
    assert observation is not None
    return store, owner, lease, observation


def rows(store, table):
    with sqlite3.connect(store.path) as conn:
        return conn.execute(f"SELECT * FROM {table}").fetchall()


def test_duplicate_callback_and_restart_preserve_failure_bytes(bound):
    store, owner, _, observation = bound
    observation.reservation.fail(store, "private diagnostic", observation=observation)
    before = store.path.read_bytes()
    for current in (store, GoalAttemptStore(store.root)):
        with pytest.raises(StaleAttemptError):
            observation.reservation.fail(current, "duplicate", observation=observation)
        with pytest.raises(UnresolvedAttemptError):
            current.resume(owner.goal.id, 1)
        assert current.require_generation(owner.goal.id).failure_evidence(current) == observation.evidence
    assert store.path.read_bytes() == before
    assert len(rows(store, "failed_turn_evidence")) == 1
    encoded = json.dumps(rows(store, "failed_turn_evidence"))
    assert observation.reservation.token not in encoded
    assert "private diagnostic" not in encoded


@pytest.mark.parametrize("mutation", [
    {"identity": TurnIdentity(RecordedThreadIncarnation("owner", 11.0), 7)},
    {"turn_id": "b" * 32}, {"claimed_generation": 4},
    {"identity": TurnIdentity(RecordedThreadIncarnation("owner", 10.0), 8)},
])
def test_mismatched_turn_lease_is_not_bound(bound, mutation):
    store, owner, lease, observation = bound
    rejected = FailedTurnObservation.from_terminal(
        observation.reservation, owner=owner, goal=owner.goal,
        lease=replace(lease, **mutation), turn_id=lease.turn_id, claim_scope=3,
        current_owner=owner, current_claim=3, reason=FailureReason.BACKEND_FAILED,
    )
    assert rejected is None
    observation.reservation.fail(store, "failed anyway", observation=rejected)
    assert store.snapshot("goal").lifecycle == BlockedGeneration()
    assert store.require_generation("goal").failure_evidence(store) == NoFailedTurnEvidence()


@pytest.mark.parametrize("binding", ["token", "evidence_identity"])
def test_mismatched_permit_cannot_attach_observation_or_weaken_failure(bound, binding):
    store, owner, _, observation = bound
    forged = (
        replace(observation, reservation=replace(observation.reservation, token="wrong"))
        if binding == "token"
        else replace(observation, evidence=replace(observation.evidence, goal_id="another"))
    )
    observation.reservation.fail(store, "failed", observation=forged)
    assert rows(store, "failed_turn_evidence") == []
    assert store.snapshot("goal").lifecycle == BlockedGeneration()
    assert store.require_generation("goal").failure_evidence(store) == NoFailedTurnEvidence()


def test_unclaimed_attempt_failure_has_no_backend_incident(bound):
    store, owner, _, observation = bound
    store.create_goal("unclaimed")
    reservation = store.reserve("unclaimed", 1)
    reservation.fail(store, "prelaunch failure", observation=replace(observation, reservation=reservation))
    assert store.snapshot("unclaimed").lifecycle == BlockedGeneration()
    assert rows(store, "failed_turn_evidence") == []


def test_stale_attempt_cannot_record_an_incident(bound):
    store, owner, _, observation = bound
    store.retire_goal("goal", expected_generation=1, attempt_id=observation.reservation.attempt_id)
    before = store.path.read_bytes()
    with pytest.raises(StaleAttemptError):
        observation.reservation.fail(store, "late", observation=observation)
    assert store.path.read_bytes() == before
    assert rows(store, "failed_turn_evidence") == []


def test_observation_insert_error_does_not_rollback_failure_fence(bound):
    store, owner, _, observation = bound
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "CREATE TRIGGER reject_observation BEFORE INSERT ON failed_turn_evidence "
            "BEGIN SELECT RAISE(ABORT,'injected observation failure'); END"
        )
    observation.reservation.fail(store, "failed", observation=observation)
    assert store.snapshot("goal").lifecycle == BlockedGeneration()
    with sqlite3.connect(store.path) as conn:
        assert AttemptRecord.one(conn, attempt_id=observation.reservation.attempt_id).phase.declared_name == "failed"
    assert rows(store, "failed_turn_evidence") == []
    with pytest.raises(UnresolvedAttemptError):
        store.resume("goal", 1)
