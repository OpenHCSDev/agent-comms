"""Windows uses SQLite and file flushes for goal state, with no replay after reopen."""

import os

import pytest

from agent_comms.field_codec import GoalId
from agent_comms.goal_attempts import GoalAttemptStore, ReservationConflictError
from agent_comms.errors import UnresolvedAttemptError
from agent_comms.goal_generation import CompletedGeneration, ReservedGeneration


@pytest.mark.skipif(os.name != "nt", reason="Windows durability path")
def test_windows_goal_attempt_can_finish_without_replay(tmp_path):
    crashed_goal_id = GoalId.mint()
    goal_id = GoalId.mint()
    root = tmp_path / "owner"
    root.mkdir()
    store = GoalAttemptStore.initialize(root)
    store.create_goal(goal_id)
    reservation = store.reserve(goal_id, 1)
    permit = store.claim_launch(reservation)
    permit.record_verified_completion(store, "valid terminal")

    reopened = GoalAttemptStore(root)
    assert reopened.snapshot(goal_id).lifecycle == CompletedGeneration()
    with pytest.raises(ReservationConflictError):
        reopened.reserve(goal_id, 1)

    store.create_goal(crashed_goal_id)
    store.reserve(crashed_goal_id, 1)
    after_crash = GoalAttemptStore(root)
    assert after_crash.snapshot(crashed_goal_id).lifecycle == ReservedGeneration()
    with pytest.raises(ReservationConflictError):
        after_crash.reserve(crashed_goal_id, 1)
    with pytest.raises(UnresolvedAttemptError):
        after_crash.ready_grant(crashed_goal_id, 1)
