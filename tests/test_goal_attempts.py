"""Disposable POSIX crash and concurrency tests for the goal-attempt ledger."""

from __future__ import annotations

from agent_comms.errors import StaleAttemptError
import multiprocessing
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from queue import Empty

import pytest

from agent_comms.turn_failure import BackendFailed

from agent_comms.attempt_recovery import RecoveryMonitorCapability
from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_commands import StopCliCommand
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import RecoveryBlocked
from agent_comms.field_codec import AttemptId, GoalId, MalformedRecord, RetryDecisionId
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goal_attempt_phase import ClaimedAttempt, FailedAttempt, SucceededAttempt
from agent_comms.goal_failure_observation import FailedTurnEvidence
from agent_comms.goal_attempts import AttemptRecord, Generation, GoalAttemptStore, GoalWorkDecision, ProviderUsageTotal, ReservationConflictError, StorageUncertainError
from agent_comms.errors import UnresolvedAttemptError
from agent_comms.goal_generation import (
    BlockedGeneration,
    CancelledGeneration,
    CompletedGeneration,
    ReadyGeneration,
    ReservedGeneration,
)
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.pi_counts import PiCount, UnreportedCount
from agent_comms.pi_payloads import PiUsage
from agent_comms.threads import Thread
from agent_comms.thread_creation import RecordedCreationTime
from agent_comms.attempt_resolution import FailedTurnResolution, OperatorStopped, RetryDecision

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX fork and directory fsync crash instrumentation."
)


def _competing_reserve(root: str, goal_id: GoalId, grant: str, start, results) -> None:
    store = GoalAttemptStore(root)
    start.wait(5)
    try:
        reservation = store.reserve(goal_id, 1, ready_grant=grant)
        results.put(("reserved", reservation.attempt_id))
    except ReservationConflictError:
        results.put(("conflict", ""))


def _die_after_reservation(root: str, goal_id: GoalId, grant: str) -> None:
    GoalAttemptStore(root).reserve(goal_id, 1, ready_grant=grant)
    os._exit(0)


def _die_after_claim(root: str, goal_id: GoalId, grant: str) -> None:
    store = GoalAttemptStore(root)
    reservation = store.reserve(goal_id, 1, ready_grant=grant)
    store.claim_launch(reservation)
    os._exit(0)


def _die_before_ready_ack(root: str, goal_id: GoalId) -> None:
    store = GoalAttemptStore(root)
    store._sync = lambda: os._exit(0)
    store.create_goal(goal_id)
    os._exit(1)


@pytest.fixture
def store(tmp_path) -> Iterator[GoalAttemptStore]:
    root = tmp_path / "owner"
    root.mkdir(mode=0o700)
    yield GoalAttemptStore.initialize(root)


def test_setup_requires_explicit_owner_private_root_and_0600_db(tmp_path):
    missing_goal_id = GoalId.mint()
    with pytest.raises(StorageUncertainError):
        GoalAttemptStore.initialize(tmp_path / "missing")
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    store = GoalAttemptStore.initialize(root)
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert GoalAttemptStore(root).snapshot(missing_goal_id) is None


def test_one_reservation_across_two_independent_supervisors(store):
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    results = ctx.Queue()
    children = [
        ctx.Process(
            target=_competing_reserve,
            args=(str(store.root), goal_id, store.ready_grant(goal_id, 1), start, results),
        )
        for _ in range(2)
    ]
    for child in children:
        child.start()
    start.set()
    outcomes = []
    try:
        for _ in children:
            outcomes.append(results.get(timeout=12)[0])
    except Empty:
        pytest.fail("A competing supervisor did not report a bounded result")
    finally:
        for child in children:
            child.join(timeout=5)
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
    assert sorted(outcomes) == ["conflict", "reserved"]
    assert all(child.exitcode == 0 for child in children)
    assert store.snapshot(goal_id).lifecycle == ReservedGeneration()


def test_claim_is_one_shot_and_reopened_store_cannot_replay(store):
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    reservation = store.reserve(goal_id, 1)
    reopened = GoalAttemptStore(store.root)
    with pytest.raises(UnresolvedAttemptError):
        reopened.claim_launch(reservation)
    permit = store.claim_launch(reservation)
    assert permit.reservation == reservation
    with pytest.raises(UnresolvedAttemptError):
        store.claim_launch(reservation)
    with pytest.raises(ReservationConflictError):
        reopened.reserve(goal_id, 1)


def test_owner_can_rotate_only_unused_ready_grant_without_changing_generation(store):
    goal_id = GoalId.mint()
    initial = store.create_goal(goal_id)
    old_grant = store.ready_grant(goal_id, 1)
    reopened = GoalAttemptStore(store.root)
    assert reopened.recover_unreserved_ready(goal_id, 1) == initial
    with pytest.raises(UnresolvedAttemptError):
        store.reserve(goal_id, 1, ready_grant=old_grant)
    attempt = reopened.reserve(goal_id, 1)
    assert attempt.generation == 1
    with pytest.raises(ReservationConflictError):
        reopened.recover_unreserved_ready(goal_id, 1)


@pytest.mark.parametrize("phase", ["reserved", "claimed", "failed", "completed"])
def test_ready_recovery_never_replays_an_existing_attempt(store, phase):
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    reservation = store.reserve(goal_id, 1)
    if phase != "reserved":
        permit = store.claim_launch(reservation)
        if phase == "failed":
            reservation.fail(store, FailedTurnResolution(BackendFailed))
        elif phase == "completed":
            permit.record_verified_completion(store, "verified terminal")
    before = store.snapshot(goal_id)
    reopened = GoalAttemptStore(store.root)
    with pytest.raises(ReservationConflictError):
        reopened.recover_unreserved_ready(goal_id, 1)
    assert reopened.snapshot(goal_id) == before


def test_crash_after_reservation_before_outcome_never_auto_reissues(store):
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(
        target=_die_after_reservation,
        args=(str(store.root), goal_id, store.ready_grant(goal_id, 1)),
    )
    child.start()
    child.join(timeout=12)
    if child.is_alive():
        child.terminate()
        child.join(timeout=5)
    assert child.exitcode == 0
    reopened = GoalAttemptStore(store.root)
    assert reopened.snapshot(goal_id).lifecycle == ReservedGeneration()
    with pytest.raises(ReservationConflictError):
        reopened.reserve(goal_id, 1)
    with pytest.raises(UnresolvedAttemptError):
        reopened.resume(goal_id, 1)


def test_claimed_crash_requires_separate_human_abandon_and_retry(store):
    _decision_0 = RetryDecision(RetryDecisionId.mint())
    _decision_1 = RetryDecision(RetryDecisionId.mint())
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(
        target=_die_after_claim,
        args=(str(store.root), goal_id, store.ready_grant(goal_id, 1)),
    )
    child.start()
    child.join(timeout=12)
    if child.is_alive():
        child.terminate()
        child.join(timeout=5)
    assert child.exitcode == 0
    reopened = GoalAttemptStore(store.root)
    current = reopened.snapshot(goal_id)
    assert current is not None and current.lifecycle == ReservedGeneration()
    assert current.attempt_id is not None
    with pytest.raises(UnresolvedAttemptError):
        reopened.resume(goal_id, 1)
    with pytest.raises(ReservationConflictError):
        reopened.reserve(goal_id, 1)
    with pytest.raises(ValueError):
        reopened.authorize_abandon_attempt(
            goal_id,
            expected_generation=1,
            attempt_id=current.attempt_id,
            decision=RetryDecision(RetryDecisionId("")),
        )
    blocked = reopened.authorize_abandon_attempt(
        goal_id,
        expected_generation=1,
        attempt_id=current.attempt_id,
        decision=_decision_0,
    )
    assert blocked.lifecycle == BlockedGeneration()
    with pytest.raises(UnresolvedAttemptError):
        reopened.resume(goal_id, 1)
    with pytest.raises(StaleAttemptError):
        reopened.authorize_abandon_attempt(
            goal_id,
            expected_generation=1,
            attempt_id=current.attempt_id,
            decision=_decision_0,
        )
    next_generation = reopened.authorize_retry(
        goal_id,
        expected_generation=1,
        attempt_id=current.attempt_id,
        decision=_decision_1,
    )
    assert next_generation.number == 2
    assert reopened.reserve(goal_id, 2).generation == 2


def test_crash_after_ready_commit_before_ack_requires_explicit_human_recovery(store):
    _decision_0 = RetryDecision(RetryDecisionId.mint())
    goal_id = GoalId.mint()
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(target=_die_before_ready_ack, args=(str(store.root), goal_id))
    child.start()
    child.join(timeout=12)
    if child.is_alive():
        child.terminate()
        child.join(timeout=5)
    assert child.exitcode == 0
    reopened = GoalAttemptStore(store.root)
    assert reopened.snapshot(goal_id).lifecycle == ReadyGeneration()
    with pytest.raises(UnresolvedAttemptError):
        reopened.reserve(goal_id, 1)
    with pytest.raises(UnresolvedAttemptError):
        reopened.resume(goal_id, 1)
    assert (
        reopened.authorize_ready_recovery(
            goal_id, expected_generation=1, decision=_decision_0
        ).number
        == 2
    )
    assert reopened.reserve(goal_id, 2).generation == 2


def test_failure_blocks_resume_until_explicit_separate_retry_decision(store):
    _decision_0 = RetryDecision(RetryDecisionId.mint())
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    reservation = store.reserve(goal_id, 1)
    store.claim_launch(reservation)
    blocked = reservation.fail(store, FailedTurnResolution(BackendFailed))
    assert blocked.lifecycle == BlockedGeneration() and blocked.attempt_id == reservation.attempt_id
    with pytest.raises(UnresolvedAttemptError):
        store.resume(goal_id, 1)
    with pytest.raises(ReservationConflictError):
        store.reserve(goal_id, 1)
    with pytest.raises(ValueError):
        store.authorize_retry(
            goal_id,
            expected_generation=1,
            attempt_id=reservation.attempt_id,
            decision=RetryDecision(RetryDecisionId("")),
        )
    resumed = store.authorize_retry(
        goal_id,
        expected_generation=1,
        attempt_id=reservation.attempt_id,
        decision=_decision_0,
    )
    assert resumed.number == 2 and resumed.lifecycle == ReadyGeneration()
    assert store.resume(goal_id, 2) == resumed
    next_attempt = store.reserve(goal_id, 2)
    assert next_attempt.attempt_id != reservation.attempt_id
    with pytest.raises(StaleAttemptError):
        reservation.fail(store, FailedTurnResolution(BackendFailed))
    assert store.snapshot(goal_id).attempt_id == next_attempt.attempt_id


def test_stale_old_done_cannot_block_or_overwrite_new_generation(store):
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    prior = store.reserve(goal_id, 1)
    permit = store.claim_launch(prior)
    next_generation = permit.record_verified_progress(store, "registry-progress-witness-1")
    assert next_generation.number == 2 and next_generation.lifecycle == ReadyGeneration()
    newer = store.reserve(goal_id, 2)
    with pytest.raises(StaleAttemptError):
        prior.fail(store, FailedTurnResolution(BackendFailed))
    with pytest.raises(StaleAttemptError):
        permit.record_verified_progress(store, "late old success")
    assert store.snapshot(goal_id).attempt_id == newer.attempt_id
    with pytest.raises(ReservationConflictError):
        store.reserve(goal_id, 2)


def test_verified_completion_is_terminal_without_a_new_ready_grant(store):
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    reservation = store.reserve(goal_id, 1)
    permit = store.claim_launch(reservation)
    completed = permit.record_verified_completion(store, "registry-completed-revision-2")
    assert completed.lifecycle == CompletedGeneration()
    assert completed.attempt_id == reservation.attempt_id
    assert store.snapshot(goal_id) == completed
    with pytest.raises((ReservationConflictError, UnresolvedAttemptError)):
        store.reserve(goal_id, completed.number)
    with pytest.raises(UnresolvedAttemptError):
        store.ready_grant(goal_id, completed.number + 1)


def test_provider_reported_responses_are_attributed_once_and_survive_reopen(store):
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    reservation = store.reserve(goal_id, 1)
    permit = store.claim_launch(reservation)
    first = PiUsage.from_wire(
        {"input": 100, "output": 20, "totalTokens": 120, "cost": {"total": 0.1}})
    second = PiUsage.from_wire(
        {"input": 40, "output": 10, "totalTokens": 50, "cost": {"total": 0.2}})

    permit.record_provider_usage(store, "response-1", first)
    permit.record_provider_usage(store, "response-1", first)
    permit.record_provider_usage(store, "response-2", second)
    with pytest.raises(ValueError):
        permit.record_provider_usage(store, "response-1", second)

    reopened = GoalAttemptStore(store.root)
    totals = ProviderUsageTotal.for_goal(reopened, goal_id)
    assert totals.responses == 2
    assert totals.input_tokens == PiCount.from_wire(140)
    assert totals.output_tokens == PiCount.from_wire(30)
    assert totals.total_tokens == PiCount.from_wire(170)
    assert str(totals.cost_total) == "0.3"


@pytest.mark.parametrize("cost", [-0.1, float("nan"), float("inf")])
def test_usage_decoding_refuses_a_cost_that_is_not_a_finite_nonnegative_number(cost):
    with pytest.raises(MalformedRecord):
        PiUsage.from_wire({"input": 1, "cost": {"total": cost}})


def test_a_negative_token_count_decodes_as_unreported():
    assert PiUsage.from_wire({"input": -1, "output": 2}).input == UnreportedCount()


@pytest.mark.parametrize("phase", ["reserved", "claimed"])
def test_clearing_goal_retires_reserved_attempt_without_replay(store, phase):
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    reservation = store.reserve(goal_id, 1)
    if phase == "claimed":
        store.claim_launch(reservation)
    retired = store.retire_goal(goal_id, expected_generation=1, attempt_id=reservation.attempt_id)
    assert retired.lifecycle == CancelledGeneration()
    assert store.snapshot(goal_id) == retired
    with pytest.raises((StaleAttemptError, UnresolvedAttemptError)):
        store.claim_launch(reservation)
    with pytest.raises((ReservationConflictError, UnresolvedAttemptError)):
        store.reserve(goal_id, 1)


def test_ready_grant_is_not_persisted_and_stale_decisions_cannot_launch(store):
    _decision_0 = RetryDecision(RetryDecisionId.mint())
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    grant = store.ready_grant(goal_id, 1)
    assert grant.encode() not in store.path.read_bytes()
    reopened = GoalAttemptStore(store.root)
    with pytest.raises(UnresolvedAttemptError):
        reopened.reserve(goal_id, 1)
    rotated = reopened.authorize_ready_recovery(
        goal_id, expected_generation=1, decision=_decision_0
    )
    assert rotated.number == 2
    assert reopened.ready_grant(goal_id, 2).encode() not in store.path.read_bytes()
    with pytest.raises(ReservationConflictError):
        store.reserve(goal_id, 1, ready_grant=grant)
    with pytest.raises(UnresolvedAttemptError):
        reopened.authorize_ready_recovery(
            goal_id, expected_generation=2, decision=_decision_0
        )
    with pytest.raises(UnresolvedAttemptError):
        reopened.reserve(goal_id, 2, ready_grant=grant)
    assert reopened.reserve(goal_id, 2).generation == 2


def test_one_human_decision_id_cannot_authorize_two_attempts(store):
    _decision_0 = RetryDecision(RetryDecisionId.mint())
    _decision_1 = RetryDecision(RetryDecisionId.mint())
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    first = store.reserve(goal_id, 1)
    first.fail(store, FailedTurnResolution(BackendFailed))
    store.authorize_retry(
        goal_id,
        expected_generation=1,
        attempt_id=first.attempt_id,
        decision=_decision_0,
    )
    second = store.reserve(goal_id, 2)
    second.fail(store, FailedTurnResolution(BackendFailed))
    with pytest.raises(UnresolvedAttemptError):
        store.authorize_retry(
            goal_id,
            expected_generation=2,
            attempt_id=second.attempt_id,
            decision=_decision_0,
        )
    assert store.snapshot(goal_id).lifecycle == BlockedGeneration()
    store.authorize_retry(
        goal_id,
        expected_generation=2,
        attempt_id=second.attempt_id,
        decision=_decision_1,
    )
    assert store.reserve(goal_id, 3).generation == 3


@pytest.fixture
def claimed_owner(comms):
    # This process cannot run turn cleanup. Only the real stop/recovery path
    # can settle the durable claim left behind by its exited owner.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True,
    )
    try:
        owner = Thread(
            "stopped-goal", frozenset(), str(comms.root),
            process_identity=ProcessIdentity.capture(child.pid),
        )
        comms.registry.declare(owner)
        comms.goals.update_goal(owner.name, SetGoalAction(text="Continue the stopped order."))
        owner = comms.registry.require(owner.name)
        store = comms.goals.open_goal_store()
        generation = store.require_generation(owner.goal.require().id)
        store.claim_launch(store.reserve(generation.goal_id, generation.number))
        yield owner
    finally:
        comms.owners.stop("stopped-goal")
        child.wait(timeout=5)


def test_stop_recovers_claim_after_owner_exits_without_turn_cleanup(comms, claimed_owner):
    store = comms.goals.open_goal_store()
    goal_id = claimed_owner.goal.require().id
    before = store.require_generation(goal_id)
    with closing(store._connect()) as conn:
        attempt = AttemptRecord.one(conn, attempt_id=before.attempt_id)
        assert attempt.phase == ClaimedAttempt()
    # Registration created the participant, not a native-input family.
    with closing(sqlite3.connect(
        CoordinationStore.path_in(comms.root).as_uri() + "?mode=ro", uri=True,
    )) as conn:
        assert not NativeRuntimeInput.present(conn)
    StopCliCommand(name=claimed_owner.name).apply(comms)

    stopped = comms.registry.require(claimed_owner.name)
    assert not stopped.process_alive
    assert comms.owners.releases.read_many((stopped.name,))[stopped.name].matches(claimed_owner)
    successor = store.require_generation(goal_id)
    assert successor.number == before.number + 1
    assert successor.lifecycle == ReadyGeneration()
    with closing(store._connect()) as conn:
        attempt = AttemptRecord.one(conn, attempt_id=before.attempt_id)
        assert attempt.phase == FailedAttempt()
        assert attempt.resolution == OperatorStopped()
        assert attempt.progress_witness is None
        assert FailedTurnEvidence.count(conn) == 0
    assert stopped.goal == claimed_owner.goal
    stopped.goal.require().require_active()
    # Repeated recovery is a no-op; it never creates another ready generation.
    RecoveryMonitorCapability.settle_stopped_owners(comms.owners, (stopped,))
    assert store.require_generation(goal_id) == successor
    # Even without native coordination, a foreign release must be rejected.
    substituted = replace(claimed_owner,
                          created_at=RecordedCreationTime(claimed_owner.created_at + 1))
    with pytest.raises(RecoveryBlocked):
        RecoveryMonitorCapability.settle_stopped_owners(comms.owners, (substituted,))
    assert store.require_generation(goal_id) == successor


def test_stop_preserves_already_verified_goal_progress(comms, claimed_owner):
    store = comms.goals.open_goal_store()
    goal_id = claimed_owner.goal.require().id
    before = store.require_generation(goal_id)
    assert before.attempt_id is not None
    permit = store.stopped_permit(goal_id).claims[0]
    successor = permit.record_verified_progress(store, "completed-native-turn")

    StopCliCommand(name=claimed_owner.name).apply(comms)

    assert store.require_generation(goal_id) == successor
    assert store.read_attempt(before.attempt_id).holds(permit.reservation, SucceededAttempt())
    assert comms.registry.require(claimed_owner.name).goal == claimed_owner.goal


def test_stopped_reserved_generation_refuses_its_missing_attempt(store):
    goal_id = GoalId.mint()
    attempt_id = AttemptId.mint()
    generation = Generation(goal_id, 1, ReservedGeneration(), attempt_id)
    with pytest.raises(StorageUncertainError, match=attempt_id):
        generation.lifecycle.stopped_permit(store, generation)


def test_incomplete_current_schema_is_not_repaired_or_regranted(store):
    goal_id = GoalId.mint()
    store.create_goal(goal_id)
    with sqlite3.connect(store.path) as conn:
        GoalWorkDecision.drop(conn)
    before = store.path.read_bytes()
    with pytest.raises(StorageUncertainError):
        GoalAttemptStore(store.root)
    with pytest.raises(StorageUncertainError):
        GoalAttemptStore.initialize(store.root)
    assert store.path.read_bytes() == before
