"""Disposable POSIX crash and concurrency tests for the goal-attempt ledger."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path
from queue import Empty

import pytest

from agent_comms.goal_attempts import (
    GoalAttemptStore,
    ReservationConflict,
    StaleAttempt,
    StorageUncertain,
    UnresolvedAttempt,
)

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX fork and directory fsync crash instrumentation."
)


def _competing_reserve(root: str, grant: str, start, results) -> None:
    store = GoalAttemptStore(root)
    start.wait(5)
    try:
        reservation = store.reserve("goal", 1, ready_grant=grant)
        results.put(("reserved", reservation.attempt_id))
    except ReservationConflict:
        results.put(("conflict", ""))


def _die_after_reservation(root: str, grant: str) -> None:
    GoalAttemptStore(root).reserve("goal", 1, ready_grant=grant)
    os._exit(0)


def _die_after_claim(root: str, grant: str) -> None:
    store = GoalAttemptStore(root)
    reservation = store.reserve("goal", 1, ready_grant=grant)
    store.claim_launch(reservation)
    os._exit(0)


def _die_before_ready_ack(root: str) -> None:
    store = GoalAttemptStore(root)
    store._sync = lambda: os._exit(0)
    store.create_goal("goal")
    os._exit(1)


@pytest.fixture
def store() -> Iterator[GoalAttemptStore]:
    with tempfile.TemporaryDirectory(prefix="ac-goal-ledger-", dir="/var/tmp") as base:
        root = Path(base) / "owner"
        root.mkdir(mode=0o700)
        yield GoalAttemptStore.initialize(root)


def test_setup_requires_explicit_owner_private_root_and_0600_db(tmp_path):
    with pytest.raises(StorageUncertain, match="owner-0700"):
        GoalAttemptStore.initialize(tmp_path / "missing")
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    store = GoalAttemptStore.initialize(root)
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert GoalAttemptStore(root).snapshot("missing") is None


def test_one_reservation_across_two_independent_supervisors(store):
    store.create_goal("goal")
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    results = ctx.Queue()
    children = [
        ctx.Process(
            target=_competing_reserve,
            args=(str(store.root), store.ready_grant("goal", 1), start, results),
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
    assert store.snapshot("goal").state == "reserved"


def test_claim_is_one_shot_and_reopened_store_cannot_replay(store):
    store.create_goal("goal")
    reservation = store.reserve("goal", 1)
    reopened = GoalAttemptStore(store.root)
    with pytest.raises(UnresolvedAttempt):
        reopened.claim_launch(reservation)
    permit = store.claim_launch(reservation)
    assert permit.reservation == reservation
    with pytest.raises(UnresolvedAttempt):
        store.claim_launch(reservation)
    with pytest.raises(ReservationConflict):
        reopened.reserve("goal", 1)


def test_crash_after_reservation_before_outcome_never_auto_reissues(store):
    store.create_goal("goal")
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(
        target=_die_after_reservation,
        args=(str(store.root), store.ready_grant("goal", 1)),
    )
    child.start()
    child.join(timeout=12)
    if child.is_alive():
        child.terminate()
        child.join(timeout=5)
    assert child.exitcode == 0
    reopened = GoalAttemptStore(store.root)
    assert reopened.snapshot("goal").state == "reserved"
    with pytest.raises(ReservationConflict):
        reopened.reserve("goal", 1)
    with pytest.raises(UnresolvedAttempt):
        reopened.resume("goal", 1)


def test_claimed_crash_requires_separate_human_abandon_and_retry(store):
    store.create_goal("goal")
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(
        target=_die_after_claim,
        args=(str(store.root), store.ready_grant("goal", 1)),
    )
    child.start()
    child.join(timeout=12)
    if child.is_alive():
        child.terminate()
        child.join(timeout=5)
    assert child.exitcode == 0
    reopened = GoalAttemptStore(store.root)
    current = reopened.snapshot("goal")
    assert current is not None and current.state == "reserved"
    assert current.attempt_id is not None
    with pytest.raises(UnresolvedAttempt):
        reopened.resume("goal", 1)
    with pytest.raises(ReservationConflict):
        reopened.reserve("goal", 1)
    with pytest.raises(ValueError, match="explicit user"):
        reopened.authorize_abandon_attempt(
            "goal", expected_generation=1, attempt_id=current.attempt_id, user_decision_id=""
        )
    blocked = reopened.authorize_abandon_attempt(
        "goal",
        expected_generation=1,
        attempt_id=current.attempt_id,
        user_decision_id="human-marked-uncertain-1",
    )
    assert blocked.state == "blocked"
    with pytest.raises(UnresolvedAttempt):
        reopened.resume("goal", 1)
    with pytest.raises(StaleAttempt):
        reopened.authorize_abandon_attempt(
            "goal",
            expected_generation=1,
            attempt_id=current.attempt_id,
            user_decision_id="human-marked-uncertain-1",
        )
    next_generation = reopened.authorize_retry(
        "goal",
        expected_generation=1,
        attempt_id=current.attempt_id,
        user_decision_id="separate-human-retry-1",
    )
    assert next_generation.number == 2
    assert reopened.reserve("goal", 2).generation == 2


def test_crash_after_ready_commit_before_ack_requires_explicit_human_recovery(store):
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(target=_die_before_ready_ack, args=(str(store.root),))
    child.start()
    child.join(timeout=12)
    if child.is_alive():
        child.terminate()
        child.join(timeout=5)
    assert child.exitcode == 0
    reopened = GoalAttemptStore(store.root)
    assert reopened.snapshot("goal").state == "ready"
    with pytest.raises(UnresolvedAttempt, match="acknowledged ready grant"):
        reopened.reserve("goal", 1)
    with pytest.raises(UnresolvedAttempt):
        reopened.resume("goal", 1)
    assert (
        reopened.authorize_ready_recovery(
            "goal", expected_generation=1, user_decision_id="human-recover-after-crash"
        ).number
        == 2
    )
    assert reopened.reserve("goal", 2).generation == 2


def test_fsync_failure_after_commit_never_returns_launch_authority(store, monkeypatch):
    store.create_goal("goal")

    def failed_sync():
        raise OSError("injected directory fsync failure after committed reservation")

    monkeypatch.setattr(store, "_sync", failed_sync)
    with pytest.raises(StorageUncertain, match="durability"):
        store.reserve("goal", 1)
    reopened = GoalAttemptStore(store.root)
    # The row is visible in the ordinary crash model; no token was returned.
    assert reopened.snapshot("goal").state == "reserved"
    with pytest.raises(ReservationConflict):
        reopened.reserve("goal", 1)


def test_launch_claim_sync_failure_is_unresolved_and_never_replayable(store, monkeypatch):
    store.create_goal("goal")
    reservation = store.reserve("goal", 1)

    def failed_sync():
        raise OSError("injected fsync failure after committed launch claim")

    monkeypatch.setattr(store, "_sync", failed_sync)
    with pytest.raises(StorageUncertain, match="durability"):
        store.claim_launch(reservation)
    reopened = GoalAttemptStore(store.root)
    assert reopened.snapshot("goal").state == "reserved"
    with pytest.raises(ReservationConflict):
        reopened.reserve("goal", 1)
    with pytest.raises(UnresolvedAttempt):
        reopened.claim_launch(reservation)
    with pytest.raises(UnresolvedAttempt):
        store.claim_launch(reservation)


def test_commit_uncertainty_never_returns_launch_authority(store, monkeypatch):
    store.create_goal("goal")

    def uncertain_commit(conn):
        conn.commit()
        raise sqlite3.OperationalError("injected lost commit ACK")

    monkeypatch.setattr(store, "_commit", uncertain_commit)
    with pytest.raises(StorageUncertain, match="durability"):
        store.reserve("goal", 1)
    assert GoalAttemptStore(store.root).snapshot("goal").state == "reserved"
    with pytest.raises(ReservationConflict):
        GoalAttemptStore(store.root).reserve("goal", 1)


def test_failure_blocks_resume_until_explicit_separate_retry_decision(store):
    store.create_goal("goal")
    reservation = store.reserve("goal", 1)
    store.claim_launch(reservation)
    blocked = store.record_failed(reservation, "outcome unknown; no automatic replay")
    assert blocked.state == "blocked" and blocked.attempt_id == reservation.attempt_id
    with pytest.raises(UnresolvedAttempt):
        store.resume("goal", 1)
    with pytest.raises(ReservationConflict):
        store.reserve("goal", 1)
    with pytest.raises(ValueError, match="explicit user"):
        store.authorize_retry(
            "goal",
            expected_generation=1,
            attempt_id=reservation.attempt_id,
            user_decision_id="",
        )
    resumed = store.authorize_retry(
        "goal",
        expected_generation=1,
        attempt_id=reservation.attempt_id,
        user_decision_id="visible-user-decision-1",
    )
    assert resumed.number == 2 and resumed.state == "ready"
    assert store.resume("goal", 2) == resumed
    next_attempt = store.reserve("goal", 2)
    assert next_attempt.attempt_id != reservation.attempt_id
    with pytest.raises(StaleAttempt):
        store.record_failed(reservation, "late previous turn")
    assert store.snapshot("goal").attempt_id == next_attempt.attempt_id


def test_stale_old_done_cannot_block_or_overwrite_new_generation(store):
    store.create_goal("goal")
    prior = store.reserve("goal", 1)
    permit = store.claim_launch(prior)
    next_generation = store.record_verified_progress(permit, "registry-progress-witness-1")
    assert next_generation.number == 2 and next_generation.state == "ready"
    newer = store.reserve("goal", 2)
    with pytest.raises(StaleAttempt):
        store.record_failed(prior, "late old failure")
    with pytest.raises(StaleAttempt):
        store.record_verified_progress(permit, "late old success")
    assert store.snapshot("goal").attempt_id == newer.attempt_id
    with pytest.raises(ReservationConflict):
        store.reserve("goal", 2)


def test_verified_completion_is_terminal_without_a_new_ready_grant(store):
    store.create_goal("goal")
    reservation = store.reserve("goal", 1)
    permit = store.claim_launch(reservation)
    completed = store.record_verified_completion(permit, "registry-completed-revision-2")
    assert completed.state == "completed"
    assert completed.attempt_id == reservation.attempt_id
    assert store.snapshot("goal") == completed
    with pytest.raises((ReservationConflict, UnresolvedAttempt)):
        store.reserve("goal", completed.number)
    with pytest.raises(UnresolvedAttempt):
        store.ready_grant("goal", completed.number + 1)


def test_provider_reported_responses_are_attributed_once_and_survive_reopen(store):
    store.create_goal("goal")
    reservation = store.reserve("goal", 1)
    permit = store.claim_launch(reservation)
    first = {"input": 100, "output": 20, "totalTokens": 120, "cost": {"total": 0.1}}
    second = {"input": 40, "output": 10, "totalTokens": 50, "cost": {"total": 0.2}}

    store.record_provider_usage(permit, "response-1", first)
    store.record_provider_usage(permit, "response-1", first)
    store.record_provider_usage(permit, "response-2", second)
    with pytest.raises(ValueError, match="different usage"):
        store.record_provider_usage(permit, "response-1", second)

    reopened = GoalAttemptStore(store.root)
    totals = reopened.provider_usage_total("goal")
    assert totals.responses == 2
    assert totals.input_tokens == 140
    assert totals.output_tokens == 30
    assert totals.total_tokens == 170
    assert str(totals.cost_total) == "0.3"


@pytest.mark.parametrize("phase", ["reserved", "claimed"])
def test_clearing_goal_retires_reserved_attempt_without_replay(store, phase):
    store.create_goal("goal")
    reservation = store.reserve("goal", 1)
    if phase == "claimed":
        store.claim_launch(reservation)
    retired = store.retire_goal("goal", expected_generation=1, attempt_id=reservation.attempt_id)
    assert retired.state == "cancelled"
    assert store.snapshot("goal") == retired
    with pytest.raises((StaleAttempt, UnresolvedAttempt)):
        store.claim_launch(reservation)
    with pytest.raises((ReservationConflict, UnresolvedAttempt)):
        store.reserve("goal", 1)


@pytest.mark.parametrize("transition", ["create", "progress", "retry"])
@pytest.mark.parametrize("failed_ack", ["fsync", "commit"])
def test_ready_writes_uncertain_after_commit_cannot_launch_after_reopen(
    store, monkeypatch, transition, failed_ack
):
    attempt_id = None
    if transition != "create":
        store.create_goal("goal")
        reservation = store.reserve("goal", 1)
        attempt_id = reservation.attempt_id
        if transition == "progress":
            permit = store.claim_launch(reservation)
        else:
            store.record_failed(reservation, "attempt outcome unknown")

    if failed_ack == "fsync":

        def lose_ack():
            raise OSError("injected sync failure AFTER COMMIT")

        monkeypatch.setattr(store, "_sync", lose_ack)
    else:

        def lose_commit_ack(conn):
            conn.commit()
            raise sqlite3.OperationalError("injected COMMIT ACK loss")

        monkeypatch.setattr(store, "_commit", lose_commit_ack)

    with pytest.raises(StorageUncertain, match="durability"):
        if transition == "create":
            store.create_goal("goal")
        elif transition == "progress":
            store.record_verified_progress(permit, "real-registry-progress-witness")
        else:
            assert attempt_id is not None
            store.authorize_retry(
                "goal",
                expected_generation=1,
                attempt_id=attempt_id,
                user_decision_id="human-retry-decision-1",
            )

    expected = 1 if transition == "create" else 2
    # COMMIT happened; post-COMMIT failures still leave a visible READY row.
    reopened = GoalAttemptStore(store.root)
    assert reopened.snapshot("goal").number == expected
    assert reopened.snapshot("goal").state == "ready"
    with pytest.raises(UnresolvedAttempt):
        store.ready_grant("goal", expected)
    with pytest.raises(UnresolvedAttempt):
        reopened.ready_grant("goal", expected)
    with pytest.raises(UnresolvedAttempt):
        reopened.reserve("goal", expected)
    with pytest.raises(UnresolvedAttempt):
        reopened.resume("goal", expected)
    recovered = reopened.authorize_ready_recovery(
        "goal",
        expected_generation=expected,
        user_decision_id=f"human-recovery-{transition}-{failed_ack}",
    )
    assert recovered.number == expected + 1
    assert reopened.reserve("goal", recovered.number).generation == expected + 1


def test_ready_grant_is_not_persisted_and_stale_decisions_cannot_launch(store):
    store.create_goal("goal")
    grant = store.ready_grant("goal", 1)
    assert grant.encode() not in store.path.read_bytes()
    reopened = GoalAttemptStore(store.root)
    with pytest.raises(UnresolvedAttempt):
        reopened.reserve("goal", 1)
    rotated = reopened.authorize_ready_recovery(
        "goal", expected_generation=1, user_decision_id="visible-human-recovery-1"
    )
    assert rotated.number == 2
    assert reopened.ready_grant("goal", 2).encode() not in store.path.read_bytes()
    with pytest.raises(ReservationConflict):
        store.reserve("goal", 1, ready_grant=grant)
    with pytest.raises(UnresolvedAttempt):
        reopened.authorize_ready_recovery(
            "goal", expected_generation=2, user_decision_id="visible-human-recovery-1"
        )
    with pytest.raises(UnresolvedAttempt):
        reopened.reserve("goal", 2, ready_grant=grant)
    assert reopened.reserve("goal", 2).generation == 2


def test_one_human_decision_id_cannot_authorize_two_attempts(store):
    store.create_goal("goal")
    first = store.reserve("goal", 1)
    store.record_failed(first, "outcome unknown")
    store.authorize_retry(
        "goal",
        expected_generation=1,
        attempt_id=first.attempt_id,
        user_decision_id="single-human-decision",
    )
    second = store.reserve("goal", 2)
    store.record_failed(second, "second outcome unknown")
    with pytest.raises(UnresolvedAttempt, match="already used"):
        store.authorize_retry(
            "goal",
            expected_generation=2,
            attempt_id=second.attempt_id,
            user_decision_id="single-human-decision",
        )
    assert store.snapshot("goal").state == "blocked"
    store.authorize_retry(
        "goal",
        expected_generation=2,
        attempt_id=second.attempt_id,
        user_decision_id="different-human-decision",
    )
    assert store.reserve("goal", 3).generation == 3


def test_recovery_postcommit_fsync_failure_revokes_old_and_new_authority(store, monkeypatch):
    store.create_goal("goal")
    old_grant = store.ready_grant("goal", 1)

    def lost_sync_ack():
        raise OSError("injected post-COMMIT fsync failure during recovery")

    monkeypatch.setattr(store, "_sync", lost_sync_ack)
    with pytest.raises(StorageUncertain, match="durability"):
        store.authorize_ready_recovery(
            "goal", expected_generation=1, user_decision_id="human-recovery-lost-ack"
        )
    reopened = GoalAttemptStore(store.root)
    assert reopened.snapshot("goal").number == 2
    with pytest.raises(UnresolvedAttempt):
        reopened.reserve("goal", 2)
    with pytest.raises(ReservationConflict):
        store.reserve("goal", 1, ready_grant=old_grant)
    with pytest.raises(UnresolvedAttempt):
        reopened.authorize_ready_recovery(
            "goal", expected_generation=2, user_decision_id="human-recovery-lost-ack"
        )
    next_generation = reopened.authorize_ready_recovery(
        "goal", expected_generation=2, user_decision_id="new-human-recovery-decision"
    )
    assert reopened.reserve("goal", next_generation.number).generation == 3


def test_legacy_v1_ready_row_is_not_upgraded_or_launched(tmp_path):
    root = tmp_path / "old-owner"
    root.mkdir(mode=0o700)
    path = root / "goal_attempts.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("INSERT INTO metadata VALUES('schema_version','1')")
        conn.execute(
            "CREATE TABLE goals (goal_id TEXT, generation INT, state TEXT, attempt_id TEXT)"
        )
        conn.execute("INSERT INTO goals VALUES('goal',1,'ready',NULL)")
    path.chmod(0o600)
    with pytest.raises(StorageUncertain, match="Unsupported"):
        GoalAttemptStore(root)
    with pytest.raises(StorageUncertain, match="Unsupported"):
        GoalAttemptStore.initialize(root)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM goals").fetchall() == [("goal", 1, "ready", None)]


def test_v2_claimed_attempt_migrates_without_regranting(tmp_path):
    root = tmp_path / "old-owner"
    root.mkdir(mode=0o700)
    path = root / "goal_attempts.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("INSERT INTO metadata VALUES('schema_version','2')")
        conn.execute(
            "CREATE TABLE goals (goal_id TEXT PRIMARY KEY,generation INTEGER NOT NULL,"
            "state TEXT NOT NULL CHECK(state IN ('ready','reserved','blocked')) ,"
            "attempt_id TEXT,ready_digest TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY,goal_id TEXT NOT NULL,"
            "generation INTEGER NOT NULL,token TEXT NOT NULL,phase TEXT NOT NULL,"
            "progress_witness TEXT,resolution TEXT,FOREIGN KEY(goal_id) REFERENCES goals(goal_id))"
        )
        conn.execute(
            "CREATE TABLE human_decisions (goal_id TEXT,decision_id TEXT,generation INTEGER,"
            "FOREIGN KEY(goal_id) REFERENCES goals(goal_id))"
        )
        conn.execute("INSERT INTO goals VALUES('goal',1,'reserved','attempt','')")
        conn.execute("INSERT INTO attempts VALUES('attempt','goal',1,'token','claimed',NULL,NULL)")
    path.chmod(0o600)

    migrated = GoalAttemptStore(root)
    assert migrated.snapshot("goal").state == "reserved"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == (
            "4",
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(ReservationConflict):
        migrated.reserve("goal", 1)
    retired = migrated.retire_goal("goal", expected_generation=1, attempt_id="attempt")
    assert retired.state == "cancelled"


def test_v3_claimed_attempt_migrates_to_usage_schema_without_regranting(store):
    store.create_goal("goal")
    reservation = store.reserve("goal", 1)
    store.claim_launch(reservation)
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TABLE provider_usage")
        conn.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")

    migrated = GoalAttemptStore(store.root)
    assert migrated.snapshot("goal").state == "reserved"
    assert migrated.provider_usage_total("goal").responses == 0
    with pytest.raises(ReservationConflict):
        migrated.reserve("goal", 1)


@pytest.mark.parametrize("transition", ["claimed", "reserved"])
def test_failure_blocks_unresolved_attempt_from_any_prelaunch_phase(store, transition):
    store.create_goal("goal")
    reservation = store.reserve("goal", 1)
    if transition == "claimed":
        store.claim_launch(reservation)
    store.record_failed(reservation, "unknown outcome")
    with pytest.raises(UnresolvedAttempt):
        store.resume("goal", 1)
    with pytest.raises(ReservationConflict):
        store.reserve("goal", 1)
