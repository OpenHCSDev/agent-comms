"""Stored lifecycle decoding must not manufacture permission from malformed rows."""

import sqlite3

import pytest

from agent_comms.goal_attempts import (
    GoalAttemptStore,
    StorageUncertainError,
    UnresolvedAttemptError,
)
from agent_comms.goal_generation import BlockedGeneration, CancelledGeneration, ReadyGeneration


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "owner"
    root.mkdir(mode=0o700)
    return GoalAttemptStore.initialize(root)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE goals SET state='unexpected', ready_digest=''",
        "UPDATE goals SET state='blocked', attempt_id=NULL, ready_digest=''",
        "UPDATE goals SET generation='not a generation'",
    ],
)
def test_invalid_saved_generation_is_uncertain_and_never_authorizes_launch(store, statement):
    store.create_goal("goal")
    with sqlite3.connect(store.path) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(statement)
    reopened = GoalAttemptStore(store.root)
    with pytest.raises(StorageUncertainError, match="Invalid persisted goal generation"):
        reopened.snapshot("goal")
    with pytest.raises(StorageUncertainError, match="Invalid persisted goal generation"):
        reopened.reserve("goal", 1)


def test_invalid_phase_rolls_back_without_consuming_a_human_decision(store):
    store.create_goal("goal")
    reservation = store.reserve("goal", 1)
    with sqlite3.connect(store.path) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute("UPDATE attempts SET phase='unexpected'")
    with pytest.raises(StorageUncertainError, match="Invalid persisted goal attempt"):
        store.authorize_abandon_attempt(
            "goal",
            expected_generation=1,
            attempt_id=reservation.attempt_id,
            user_decision_id="explicit-abandon",
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM human_decisions").fetchone() == (0,)
        assert conn.execute("SELECT state FROM goals").fetchone() == ("reserved",)


def test_current_saved_failure_and_history_survive_retry_and_retirement(store):
    store.create_goal("goal")
    attempt = store.reserve("goal", 1)
    store.claim_launch(attempt)
    store.record_failed(attempt, "provider outcome UNKNOWN")
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
        assert conn.execute(
            "SELECT attempt_id,generation,phase,resolution FROM attempts"
        ).fetchall() == [(attempt.attempt_id, 1, "resolved", "explicit-retry")]
        assert conn.execute("SELECT decision_id,generation FROM human_decisions").fetchall() == [
            ("explicit-retry", 1)
        ]
    with pytest.raises(UnresolvedAttemptError):
        reopened.ready_grant("goal", 2)
