"""The ledger answers each attempt read with one member: a record, a missing one, or none by design."""

from __future__ import annotations

from agent_comms.field_codec import GoalId
from agent_comms.errors import StaleAttemptError
import os
import tempfile
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.goal_attempt_identity import GoalAttemptIdentity
from agent_comms.goal_attempt_phase import ReservedAttempt, ResolvedAttempt
from agent_comms.goal_attempts import AttemptRecord, GoalAttemptStore, NoAttempt, RecordedAttempt, StorageUncertainError
from agent_comms.goal_generation import CancelledGeneration, ReadyGeneration, ReservedGeneration

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX owner-private ledger root.")


@pytest.fixture
def store() -> Iterator[GoalAttemptStore]:
    with tempfile.TemporaryDirectory(prefix="ac-ledger-attempt-", dir="/var/tmp") as base:
        root = Path(base) / "owner"
        root.mkdir(mode=0o700)
        yield GoalAttemptStore.initialize(root)


def read(store: GoalAttemptStore, attempt_id: str):
    with closing(store._connect()) as conn:
        return AttemptRecord.read_in(conn, attempt_id)


def test_a_recorded_attempt_answers_for_its_own_reservation_only(store):
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    reservation = store.reserve(goal_id, 1)
    attempt = read(store, reservation.attempt_id)
    assert type(attempt) is RecordedAttempt
    refusal = StaleAttemptError("not this reservation")
    assert attempt.reserved_by(reservation, refusal).reservation == reservation
    with pytest.raises(StaleAttemptError):
        attempt.reserved_by(replace(reservation, token="other"), refusal)
    with pytest.raises(StaleAttemptError):
        attempt.identified(GoalAttemptIdentity(goal_id, 2, reservation.attempt_id), refusal)
    assert attempt.holds(reservation, ReservedAttempt())
    assert not attempt.resolved_as(ResolvedAttempt(), "goal cleared")
    store.retire_goal(goal_id, expected_generation=1, attempt_id=reservation.attempt_id)
    assert read(store, reservation.attempt_id).resolved_as(ResolvedAttempt(), "goal cleared")


def test_a_missing_attempt_refuses_every_question_and_cannot_be_retired(store):
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    reservation = store.reserve(goal_id, 1)
    attempt = read(store, "0" * 32)
    with pytest.raises(StorageUncertainError, match="0" * 32):
        attempt.stopped_permit()
    with pytest.raises(StaleAttemptError):
        attempt.reserved_by(reservation, StaleAttemptError("absent"))
    with pytest.raises(StaleAttemptError):
        attempt.identified(reservation.identity, StaleAttemptError("absent"))
    assert not attempt.holds(reservation, ReservedAttempt())
    assert not attempt.resolved_as(ResolvedAttempt(), "goal cleared")
    with closing(store._connect()) as conn, pytest.raises(StaleAttemptError):
        attempt.retire(conn, ReservedGeneration(), goal_id, 1)


def test_no_attempt_retires_a_ready_goal_and_refuses_a_reserved_one(store):
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    with closing(store._connect()) as conn, pytest.raises(StaleAttemptError):
        NoAttempt().retire(conn, ReservedGeneration(), goal_id, 1)
    with closing(store._connect()) as conn:
        NoAttempt().retire(conn, ReadyGeneration(), goal_id, 1)
    retired = store.retire_goal(goal_id, expected_generation=1, attempt_id=None)
    assert retired.lifecycle == CancelledGeneration()
