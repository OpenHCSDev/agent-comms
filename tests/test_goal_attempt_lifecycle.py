"""Stored lifecycle decoding must not manufacture permission from malformed rows."""

import sqlite3

import pytest

from agent_comms.goal_attempts import (
    AttemptRecord,
    GoalAttemptStore,
    GoalHumanDecision,
    UnresolvedAttemptError,
)
from agent_comms.goal_generation import BlockedGeneration, CancelledGeneration, ReadyGeneration


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "owner"
    root.mkdir(mode=0o700)
    return GoalAttemptStore.initialize(root)


def test_current_saved_failure_and_history_survive_retry_and_retirement(store):
    store.create_goal("goal")
    attempt = store.reserve("goal", 1)
    store.claim_launch(attempt)
    attempt.fail(store, "provider outcome UNKNOWN")
    reopened = GoalAttemptStore(store.root)
    assert reopened.snapshot("goal").lifecycle == BlockedGeneration()
    with pytest.raises(UnresolvedAttemptError):
        reopened.resume("goal", 1)
    successor = reopened.authorize_retry(
        "goal",
        expected_generation=1,
        attempt_id=attempt.attempt_id,
        user_decision_id="explicit-retry",
    )
    assert successor.lifecycle == ReadyGeneration()
    reopened.retire_goal("goal", expected_generation=2, attempt_id=None)
    final = GoalAttemptStore(store.root)
    assert final.snapshot("goal").lifecycle == CancelledGeneration()
    with sqlite3.connect(store.path) as conn:
        attempts = AttemptRecord.select(conn)
        assert [(row.reservation, row.phase.declared_name, row.resolution) for row in attempts] == [
            (attempt, "resolved", "explicit-retry")
        ]
        decisions = GoalHumanDecision.select(conn)
        assert [(row.decision_id, row.generation) for row in decisions] == [("explicit-retry", 1)]
    with pytest.raises(UnresolvedAttemptError):
        reopened.ready_grant("goal", 2)
