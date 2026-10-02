"""Passive observations never reopen attempts, inputs, or owner execution."""

import json
import multiprocessing
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
    StorageUncertainError,
    UnresolvedAttemptError,
)
from agent_comms.goal_failure_observation import FailedTurnObservation, read_failed_turn_projection
from agent_comms.goal_generation import BlockedGeneration, ReservedGeneration
from agent_comms.goal_states import BlockedGoal, ModelPause, OwnerPause, PausedGoal
from agent_comms.goals import Goal
from agent_comms.thread_identity import ThreadIncarnation, TurnIdentity
from agent_comms.thread_status import (
    ArchivedThreadStatus,
    DeletingThreadStatus,
    IdleThreadStatus,
    StoppedThreadStatus,
)
from agent_comms.threads import Thread
from agent_comms.turn_lease import TurnLeaseFence


@pytest.fixture
def bound(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = GoalAttemptStore.initialize(root)
    goal = Goal("private goal prose", "goal", revision=2)
    owner = Thread(
        "owner",
        frozenset(),
        "/private-worktree",
        process_identity=ProcessIdentity.capture(os.getpid()),
        created_at=10.0,
        goal=goal,
        turn_generation=7,
        last_finished_turn_id="a" * 32,
    )
    store.create_goal(goal.id)
    permit = store.claim_launch(store.reserve(goal.id, 1))
    lease = TurnLeaseFence(TurnIdentity(owner.incarnation, 7), "a" * 32, 3)
    observation = FailedTurnObservation.from_terminal(
        permit.reservation,
        owner=owner,
        goal=goal,
        lease=lease,
        turn_id=lease.turn_id,
        admission=3,
        current_owner=owner,
        current_admission=3,
        reason=FailureReason.FINAL_STOP_MISSING,
    )
    assert observation is not None
    return store, owner, lease, observation


def rows(store, table):
    with sqlite3.connect(store.path) as conn:
        return conn.execute(f"SELECT * FROM {table}").fetchall()


def blocked(owner):
    return replace(
        owner, goal=replace(owner.goal, state=BlockedGoal("Explicit fixture refusal"), revision=3)
    )


def read(store, owner, **kwargs):
    return read_failed_turn_projection(
        store.path, owner=owner, owner_status=IdleThreadStatus(), admission=3, **kwargs
    )


def test_duplicate_callback_and_restart_preserve_failure_bytes(bound):
    store, owner, _, observation = bound
    observation.reservation.fail(store, "private diagnostic", observation=observation)
    before = store.path.read_bytes()
    for current in (store, GoalAttemptStore(store.root)):
        with pytest.raises(StaleAttemptError):
            observation.reservation.fail(current, "duplicate", observation=observation)
        with pytest.raises(UnresolvedAttemptError):
            current.resume(owner.goal.id, 1)
        projection = read(current, blocked(owner))
        assert projection.to_primitive() == {
            "schema": 1,
            "state": "backend_suspended",
            "reason": "assistant_final_stop_missing",
        }
    assert store.path.read_bytes() == before
    assert len(rows(store, "failed_turn_evidence")) == 1
    encoded = json.dumps(rows(store, "failed_turn_evidence"))
    assert observation.reservation.token not in encoded
    assert "private diagnostic" not in encoded


@pytest.mark.parametrize(
    "mutation",
    [
        {"identity": TurnIdentity(ThreadIncarnation("other", 10.0), 7)},
        {"identity": TurnIdentity(ThreadIncarnation("owner", 11.0), 7)},
        {"turn_id": "b" * 32},
        {"admission_generation": 4},
        {"identity": TurnIdentity(ThreadIncarnation("owner", 10.0), 0)},
        {"identity": TurnIdentity(ThreadIncarnation("owner", 10.0), 8)},
    ],
)
def test_mismatched_turn_lease_is_not_bound(bound, mutation):
    store, owner, lease, observation = bound
    rejected = FailedTurnObservation.from_terminal(
        observation.reservation,
        owner=owner,
        goal=owner.goal,
        lease=replace(lease, **mutation),
        turn_id=lease.turn_id,
        admission=3,
        current_owner=owner,
        current_admission=3,
        reason=FailureReason.BACKEND_FAILED,
    )
    assert rejected is None
    observation.reservation.fail(store, "failed anyway", observation=rejected)
    assert store.snapshot("goal").lifecycle == BlockedGeneration()
    assert read(store, blocked(owner)).reason == "missing_binding"


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
    assert read(store, blocked(owner)).state == "unavailable"


def test_unclaimed_attempt_failure_has_no_backend_incident(bound):
    store, owner, _, observation = bound
    store.create_goal("unclaimed")
    reservation = store.reserve("unclaimed", 1)
    reservation.fail(
        store, "prelaunch failure", observation=replace(observation, reservation=reservation)
    )
    assert store.snapshot("unclaimed").lifecycle == BlockedGeneration()
    assert rows(store, "failed_turn_evidence") == []


def test_stale_attempt_cannot_record_an_incident(bound):
    store, owner, _, observation = bound
    store.retire_goal("goal", expected_generation=1, attempt_id=observation.reservation.attempt_id)
    before = store.path.read_bytes()
    with pytest.raises(StaleAttemptError):
        observation.reservation.fail(store, "late", observation=observation)
    assert store.path.read_bytes() == before
    assert read(store, blocked(owner)).state == "unavailable"


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
        assert (
            AttemptRecord.one(
                conn, attempt_id=observation.reservation.attempt_id
            ).phase.declared_name
            == "failed"
        )
    assert rows(store, "failed_turn_evidence") == []
    with pytest.raises(UnresolvedAttemptError):
        store.resume("goal", 1)
    assert read(store, blocked(owner)).reason == "unsupported_schema"


@pytest.mark.parametrize("after_commit", [False, True])
def test_commit_or_sync_error_never_yields_execution_success(bound, monkeypatch, after_commit):
    store, owner, _, observation = bound

    def fail(*args):
        raise OSError("injected storage failure")

    monkeypatch.setattr(store, "_sync" if after_commit else "_commit", fail)
    with pytest.raises(StorageUncertainError):
        observation.reservation.fail(store, "failed", observation=observation)
    reopened = GoalAttemptStore(store.root)
    assert reopened.snapshot("goal").lifecycle == (
        BlockedGeneration() if after_commit else ReservedGeneration()
    )
    assert len(rows(reopened, "failed_turn_evidence")) == int(after_commit)
    with pytest.raises(UnresolvedAttemptError):
        reopened.resume("goal", 1)
    assert read(reopened, blocked(owner)).state == (
        "backend_suspended" if after_commit else "unavailable"
    )


@pytest.mark.parametrize("source", [OwnerPause(), ModelPause()])
def test_pause_projection_never_becomes_runnable(bound, source):
    store, owner, _, observation = bound
    observation.reservation.fail(store, "failed", observation=observation)
    owner = replace(
        owner,
        goal=replace(
            owner.goal,
            state=PausedGoal(source),
            revision=3,
        ),
    )
    before = store.path.read_bytes()
    projection = read_failed_turn_projection(
        store.path, owner=owner, owner_status=IdleThreadStatus(), admission=3
    )
    assert projection.state == (
        "paused_uncertain" if isinstance(source, ModelPause) else "owner_paused"
    )
    assert "canRetry" not in projection.to_primitive()
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "mutation",
    [
        {"name": "replacement"},
        {"created_at": 11.0},
        {"worktree": "/changed"},
        {"process_identity": None},
        {"goal": Goal("replacement", "replacement", state=BlockedGoal("Explicit fixture refusal"))},
        {"goal": Goal("active", "goal")},
        {"turn_generation": 8},
        {"last_finished_turn_id": "b" * 32},
    ],
)
def test_replaced_stopped_or_active_owner_has_no_projection(bound, mutation):
    store, owner, _, observation = bound
    observation.reservation.fail(store, "failed", observation=observation)
    assert read(store, replace(blocked(owner), **mutation)).state == "unavailable"


@pytest.mark.parametrize("mode", ["missing", "invalid", "invalid_schema", "wal"])
def test_reader_never_creates_repairs_or_migrates(bound, tmp_path, mode):
    store, owner, _, observation = bound
    observation.reservation.fail(store, "failed", observation=observation)
    if mode == "missing":
        path = tmp_path / "absent" / "goal_attempts.sqlite3"
    else:
        path = store.path
        if mode == "invalid":
            path.write_bytes(b"not sqlite")
        else:
            with sqlite3.connect(path) as conn:
                if mode == "invalid_schema":
                    conn.execute("DROP TABLE goal_attempt_schema")
                else:
                    conn.execute("PRAGMA journal_mode=WAL")
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert (
        read_failed_turn_projection(
            path, owner=blocked(owner), owner_status=IdleThreadStatus(), admission=3
        ).state
        == "unavailable"
    )
    assert {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def _crash_recording(root, observation, after_commit):
    store = GoalAttemptStore(root)

    def crash(*args):
        os._exit(17)

    setattr(store, "_sync" if after_commit else "_commit", crash)
    observation.reservation.fail(store, "crashed failure", observation=observation)


@pytest.mark.parametrize("after_commit", [False, True])
def test_process_crash_has_no_partial_incident_or_replay_right(bound, after_commit):
    store, owner, _, observation = bound
    child = multiprocessing.get_context("spawn").Process(
        target=_crash_recording, args=(store.root, observation, after_commit)
    )
    child.start()
    child.join(10)
    try:
        assert child.exitcode == 17
    finally:
        if child.is_alive():
            child.kill()
            child.join(5)
    before = {p.name: p.read_bytes() for p in store.root.iterdir() if p.is_file()}
    projection = read(store, blocked(owner))
    assert projection.state == ("backend_suspended" if after_commit else "unavailable")
    assert {p.name: p.read_bytes() for p in store.root.iterdir() if p.is_file()} == before
    recovered = GoalAttemptStore(store.root)
    assert recovered.snapshot("goal").lifecycle == (
        BlockedGeneration() if after_commit else ReservedGeneration()
    )
    assert len(rows(recovered, "failed_turn_evidence")) == int(after_commit)
    with pytest.raises(UnresolvedAttemptError):
        recovered.resume("goal", 1)


@pytest.mark.parametrize(
    "status", [StoppedThreadStatus(), ArchivedThreadStatus(), DeletingThreadStatus()]
)
def test_stopped_status_is_unavailable_even_with_retained_pid(bound, status):
    store, owner, _, observation = bound
    observation.reservation.fail(store, "failed", observation=observation)
    assert (
        read_failed_turn_projection(
            store.path, owner=blocked(owner), owner_status=status, admission=3
        ).state
        == "unavailable"
    )
