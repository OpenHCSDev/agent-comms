"""A failed attempt blocks its goal until a recorded retry decision, and the store keeps that history through retirement."""

import sqlite3

import pytest

from agent_comms.errors import UnresolvedAttemptError
from agent_comms.execution.turn_failure import BackendFailed, FailedTurnResolution
from agent_comms.nominal.field_codec import GoalId, NoAttemptId, RetryDecisionId
from agent_comms.planning.attempt_resolution import DecidedResolution, RetryDecision
from agent_comms.planning.goal_attempts import GoalAttemptStore
from agent_comms.planning.goal_generation import (
    BlockedGeneration,
    CancelledGeneration,
    ReadyGeneration,
)
from agent_comms.planning.goal_ledger import AttemptRecord, GoalWorkDecision


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "owner"
    root.mkdir(mode=0o700)
    return GoalAttemptStore.initialize(root)


def test_current_saved_failure_and_history_survive_retry_and_retirement(store):
    decision = RetryDecision(RetryDecisionId.mint())
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    attempt = store.reserve(goal_id, 1)
    with sqlite3.connect(store.path) as conn:
        # Declaring absence must not change the retained SQL NULL representation.
        assert conn.execute("SELECT resolution FROM attempt_record").fetchone() == (None,)
    store.claim_launch(attempt)
    attempt.fail(store, FailedTurnResolution(BackendFailed))
    reopened = GoalAttemptStore.at(store.root)
    assert reopened.snapshot(goal_id).lifecycle == BlockedGeneration()
    with pytest.raises(UnresolvedAttemptError):
        reopened.resume(goal_id, 1)
    successor = reopened.authorize_retry(
        goal_id,
        expected_generation=1,
        attempt_id=attempt.attempt_id,
        decision=decision,
    )
    assert successor.lifecycle == ReadyGeneration()
    reopened.retire_goal(goal_id, expected_generation=2, attempt_id=NoAttemptId())
    final = GoalAttemptStore.at(store.root)
    assert final.snapshot(goal_id).lifecycle == CancelledGeneration()
    with sqlite3.connect(store.path) as conn:
        attempts = AttemptRecord.select(conn)
        assert [(row.reservation, row.phase.declared_name, row.resolution) for row in attempts] == [
            (attempt, "resolved", DecidedResolution(decision))
        ]
        decisions = GoalWorkDecision.select(conn)
        assert [(row.decision, row.generation) for row in decisions] == [(decision, 1)]
    with pytest.raises(UnresolvedAttemptError):
        reopened.ready_grant(goal_id, 2)
