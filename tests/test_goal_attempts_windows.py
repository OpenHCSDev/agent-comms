"""Windows uses SQLite and file flushes for goal state, with no replay after reopen."""

import os

import pytest

from agent_comms.goal_attempts import (
    GoalAttemptStore,
    ReservationConflict,
    UnresolvedAttempt,
)


@pytest.mark.skipif(os.name != "nt", reason="Windows durability path")
def test_windows_goal_attempt_can_finish_without_replay(tmp_path):
    root = tmp_path / "owner"
    root.mkdir()
    store = GoalAttemptStore.initialize(root)
    store.create_goal("goal")
    reservation = store.reserve("goal", 1)
    permit = store.claim_launch(reservation)
    store.record_verified_completion(permit, "valid terminal")

    reopened = GoalAttemptStore(root)
    assert reopened.snapshot("goal").state == "completed"
    with pytest.raises(ReservationConflict):
        reopened.reserve("goal", 1)

    store.create_goal("crashed")
    store.reserve("crashed", 1)
    after_crash = GoalAttemptStore(root)
    assert after_crash.snapshot("crashed").state == "reserved"
    with pytest.raises(ReservationConflict):
        after_crash.reserve("crashed", 1)
    with pytest.raises(UnresolvedAttempt):
        after_crash.ready_grant("crashed", 1)
