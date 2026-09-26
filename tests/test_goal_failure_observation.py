"""Passive observations never reopen attempts, inputs, or owner execution."""

import json
import multiprocessing
import os
import sqlite3
from dataclasses import replace

import pytest

from agent_comms.declarations import Goal, GoalPauseSource, Thread, ThreadStatus, TurnClaimFence
from agent_comms.diagnostics import FailureReason
from agent_comms.goal_attempts import (
    GoalAttemptStore,
    StaleAttempt,
    StorageUncertain,
    UnresolvedAttempt,
)
from agent_comms.goal_failure_observation import FailedTurnObservation, read_failed_turn_projection
from agent_comms.goal_pauses import GoalPauseEvent


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
        pid=123,
        created_at=10.0,
        goal=goal,
        turn_generation=7,
        last_finished_turn_id="a" * 32,
    )
    store.create_goal(goal.id)
    permit = store.claim_launch(store.reserve(goal.id, 1))
    claim = TurnClaimFence(owner.name, owner.created_at, "a" * 32, 7, 3)
    observation = FailedTurnObservation.from_terminal(
        permit.reservation,
        owner=owner,
        goal=goal,
        claim=claim,
        turn_id=claim.turn_id,
        admission=3,
        current_owner=owner,
        current_admission=3,
        reason=FailureReason.FINAL_STOP_MISSING,
    )
    assert observation is not None
    return store, owner, claim, observation


def rows(store, table):
    with sqlite3.connect(store.path) as conn:
        return conn.execute(f"SELECT * FROM {table}").fetchall()


def blocked(owner):
    return replace(owner, goal=replace(owner.goal, status="blocked", revision=3))


def read(store, owner, **kwargs):
    return read_failed_turn_projection(
        store.path, owner=owner, owner_status=ThreadStatus.IDLE, admission=3, pause=None, **kwargs
    )


def test_duplicate_callback_and_restart_preserve_failure_bytes(bound):
    store, owner, _, observation = bound
    store.record_failed(observation.reservation, "private diagnostic", observation=observation)
    before = store.path.read_bytes()
    for current in (store, GoalAttemptStore(store.root)):
        with pytest.raises(StaleAttempt):
            current.record_failed(observation.reservation, "duplicate", observation=observation)
        with pytest.raises(UnresolvedAttempt):
            current.resume(owner.goal.id, 1)
        projection = read(current, blocked(owner))
        assert projection.to_primitive() == {
            "schema": 1,
            "state": "backend_suspended",
            "reason": "assistant_final_stop_missing",
        }
    assert store.path.read_bytes() == before
    assert len(rows(store, "failed_turn_observations")) == 1
    encoded = json.dumps(rows(store, "failed_turn_observations"))
    assert observation.reservation.token not in encoded
    assert "private diagnostic" not in encoded


@pytest.mark.parametrize(
    "mutation",
    [
        {"name": "other"},
        {"created_at": 11.0},
        {"turn_id": "b" * 32},
        {"admission_generation": 4},
        {"turn_generation": 0},
        {"turn_generation": 8},
    ],
)
def test_mismatched_turn_claim_is_not_bound(bound, mutation):
    store, owner, claim, observation = bound
    rejected = FailedTurnObservation.from_terminal(
        observation.reservation,
        owner=owner,
        goal=owner.goal,
        claim=replace(claim, **mutation),
        turn_id=claim.turn_id,
        admission=3,
        current_owner=owner,
        current_admission=3,
        reason=FailureReason.BACKEND_FAILED,
    )
    assert rejected is None
    store.record_failed(observation.reservation, "failed anyway", observation=rejected)
    assert store.snapshot("goal").state == "blocked"
    assert read(store, blocked(owner)).reason == "missing_binding"


def test_mismatched_permit_cannot_attach_observation_or_weaken_failure(bound):
    store, owner, _, observation = bound
    forged = replace(observation, reservation=replace(observation.reservation, token="wrong"))
    store.record_failed(observation.reservation, "failed", observation=forged)
    assert rows(store, "failed_turn_observations") == []
    assert store.snapshot("goal").state == "blocked"
    assert read(store, blocked(owner)).state == "unavailable"


def test_unclaimed_attempt_failure_has_no_backend_incident(bound):
    store, owner, _, observation = bound
    store.create_goal("unclaimed")
    reservation = store.reserve("unclaimed", 1)
    store.record_failed(
        reservation, "prelaunch failure", observation=replace(observation, reservation=reservation)
    )
    assert store.snapshot("unclaimed").state == "blocked"
    assert rows(store, "failed_turn_observations") == []


def test_stale_attempt_cannot_record_an_incident(bound):
    store, owner, _, observation = bound
    store.retire_goal("goal", expected_generation=1, attempt_id=observation.reservation.attempt_id)
    before = store.path.read_bytes()
    with pytest.raises(StaleAttempt):
        store.record_failed(observation.reservation, "late", observation=observation)
    assert store.path.read_bytes() == before
    assert read(store, blocked(owner)).state == "unavailable"


def test_observation_insert_error_does_not_rollback_failure_fence(bound):
    store, owner, _, observation = bound
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "CREATE TRIGGER reject_observation BEFORE INSERT ON failed_turn_observations "
            "BEGIN SELECT RAISE(ABORT,'injected observation failure'); END"
        )
    store.record_failed(observation.reservation, "failed", observation=observation)
    assert store.snapshot("goal").state == "blocked"
    assert rows(store, "attempts")[0][4] == "failed"
    assert rows(store, "failed_turn_observations") == []
    with pytest.raises(UnresolvedAttempt):
        store.resume("goal", 1)
    assert read(store, blocked(owner)).reason == "missing_binding"


@pytest.mark.parametrize("after_commit", [False, True])
def test_commit_or_sync_error_never_yields_execution_success(bound, monkeypatch, after_commit):
    store, owner, _, observation = bound

    def fail(*args):
        raise OSError("injected storage failure")

    monkeypatch.setattr(store, "_sync" if after_commit else "_commit", fail)
    with pytest.raises(StorageUncertain):
        store.record_failed(observation.reservation, "failed", observation=observation)
    reopened = GoalAttemptStore(store.root)
    assert reopened.snapshot("goal").state == ("blocked" if after_commit else "reserved")
    assert len(rows(reopened, "failed_turn_observations")) == int(after_commit)
    with pytest.raises(UnresolvedAttempt):
        reopened.resume("goal", 1)
    assert read(reopened, blocked(owner)).state == (
        "backend_suspended" if after_commit else "unavailable"
    )


@pytest.mark.parametrize("source", [None, "stale", GoalPauseSource.OWNER, GoalPauseSource.MODEL])
def test_pause_projection_never_becomes_runnable(bound, source):
    store, owner, _, observation = bound
    store.record_failed(observation.reservation, "failed", observation=observation)
    owner = replace(owner, goal=replace(owner.goal, status="paused", revision=3))
    pause = (
        None
        if source is None
        else GoalPauseEvent(
            "goal",
            2 if source == "stale" else 3,
            GoalPauseSource.OWNER if source == "stale" else source,
        )
    )
    before = store.path.read_bytes()
    projection = read_failed_turn_projection(
        store.path, owner=owner, owner_status=ThreadStatus.IDLE, admission=3, pause=pause
    )
    assert projection.state == (
        "owner_paused" if source is GoalPauseSource.OWNER else "paused_uncertain"
    )
    assert "canRetry" not in projection.to_primitive()
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "mutation",
    [
        {"name": "replacement"},
        {"created_at": 11.0},
        {"worktree": "/changed"},
        {"pid": 0},
        {"goal": Goal("replacement", "replacement", status="blocked")},
        {"goal": Goal("active", "goal")},
        {"turn_generation": 8},
        {"last_finished_turn_id": "b" * 32},
    ],
)
def test_replaced_stopped_or_active_owner_has_no_projection(bound, mutation):
    store, owner, _, observation = bound
    store.record_failed(observation.reservation, "failed", observation=observation)
    assert read(store, replace(blocked(owner), **mutation)).state == "unavailable"


@pytest.mark.parametrize("mode", ["missing", "invalid", "old_schema", "wal"])
def test_reader_never_creates_repairs_or_migrates(bound, tmp_path, mode):
    store, owner, _, observation = bound
    store.record_failed(observation.reservation, "failed", observation=observation)
    if mode == "missing":
        path = tmp_path / "absent" / "goal_attempts.sqlite3"
    else:
        path = store.path
        if mode == "invalid":
            path.write_bytes(b"not sqlite")
        else:
            with sqlite3.connect(path) as conn:
                if mode == "old_schema":
                    conn.execute("UPDATE metadata SET value='4' WHERE key='schema_version'")
                else:
                    conn.execute("PRAGMA journal_mode=WAL")
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert (
        read_failed_turn_projection(
            path, owner=blocked(owner), owner_status=ThreadStatus.IDLE, admission=3, pause=None
        ).state
        == "unavailable"
    )
    assert {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_v4_migration_preserves_original_tables_without_backfilling(bound):
    store, owner, _, observation = bound
    store.record_failed(observation.reservation, "historical failed attempt")
    tables = ("goals", "attempts", "human_decisions", "provider_usage")
    before = {table: rows(store, table) for table in tables}
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TABLE failed_turn_observations")
        conn.execute("UPDATE metadata SET value='4' WHERE key='schema_version'")
    assert read(store, blocked(owner)).reason == "unsupported_schema"
    migrated = GoalAttemptStore(store.root)
    assert {table: rows(migrated, table) for table in tables} == before
    assert rows(migrated, "failed_turn_observations") == []
    assert read(migrated, blocked(owner)).reason == "missing_binding"


@pytest.mark.parametrize(
    "outcome", ["failed_done", "eof", "observation_error", "observation_rollback"]
)
@pytest.mark.parametrize("owner_pauses", [False, True])
async def test_acp_terminal_binding_retains_inputs_pause_and_no_schedule(
    wired, tmp_path, monkeypatch, outcome, owner_pauses
):
    from agent_comms.acp import CommsAgent
    from test_acp import TestAgentTurn as GoalFixture

    agent = CommsAgent(wired, agent_bin="pi")
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    await agent.new_session(str(tmp_path / "project"))
    goal = wired.update_goal("project", "set", text="private goal")
    GoalFixture()._authorize_test_goal(agent, wired, goal)
    store = agent._goal_store
    admission = wired.registry.snapshot().admission_generations["project"]
    for state in ("unknown", "started"):
        key = f"acp:earlier-{state}"
        agent._dispositions.record(
            key,
            seq=None,
            owner="project",
            admission=admission,
            target="project",
            text="private earlier input",
        )
        if state == "started":
            binding = dict(turn_id="b" * 32, native_id="c" * 32, text="private native text")
            assert agent._dispositions.bind(key, admission=admission, **binding)
            assert agent._dispositions.started(key, **binding)
    ledger_before = agent._dispositions.path.read_bytes()
    cursors = wired.root / "acp_delivery_cursors.json"
    cursor_before = cursors.read_bytes() if cursors.exists() else None
    if outcome in {"observation_error", "observation_rollback"}:
        action = "ROLLBACK" if outcome == "observation_rollback" else "ABORT"
        with sqlite3.connect(store.path) as conn:
            conn.execute(
                "CREATE TRIGGER reject_observation BEFORE INSERT ON failed_turn_observations "
                f"BEGIN SELECT RAISE({action},'injected error'); END"
            )
    pause_bytes = None

    async def failed_events(*args, **kwargs):
        nonlocal pause_bytes
        if owner_pauses:
            wired.update_goal("project", "paused", goal_id=goal.id, owner_action=True)
            pause_bytes = (wired.root / "goal_pause_events.json").read_bytes()
        yield {"type": "settled"}
        if outcome != "eof":
            yield {
                "type": "done",
                "ok": False,
                "text": "private provider text",
                "reason_code": "assistant_final_stop_missing",
                "diagnostic": {"exit_code": 0},
            }

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", failed_events)
    try:
        if outcome == "observation_rollback":
            with pytest.raises(StorageUncertain):
                await agent._run_agent_turn("project", "project", "Continue", autonomous_goal=True)
        else:
            await agent._run_agent_turn("project", "project", "Continue", autonomous_goal=True)
        assert store.snapshot(goal.id).state == "blocked"
        with pytest.raises(UnresolvedAttempt):
            store.resume(goal.id, 1)
        owner = wired.registry.require("project")
        expected = "owner_paused" if owner_pauses else "backend_suspended"
        if outcome in {"observation_error", "observation_rollback"}:
            expected = "unavailable"
        before = store.path.read_bytes()
        projection = read_failed_turn_projection(
            store.path,
            owner=owner,
            owner_status=wired.registry.snapshot().statuses["project"],
            admission=admission,
            pause=wired.goal_pause("project"),
        )
        assert projection.state == expected
        assert store.path.read_bytes() == before
        assert agent._dispositions.path.read_bytes() == ledger_before
        assert (cursors.read_bytes() if cursors.exists() else None) == cursor_before
        if owner_pauses:
            assert owner.goal.status == "paused"
            assert (wired.root / "goal_pause_events.json").read_bytes() == pause_bytes
        else:
            assert owner.goal.status == "blocked"
        agent._schedule_goal("project")
        assert not agent._pending_turns.get("project")
        observations = rows(store, "failed_turn_observations")
        assert len(observations) == (
            0 if outcome in {"observation_error", "observation_rollback"} else 1
        )
        if observations:
            terminal = json.loads(next((wired.root / "diagnostics").glob("*.json")).read_text())
            assert observations[0][9] == terminal["turn_id"]
            assert observations[0][10] == terminal["reason"]
            assert observations[0][1:3] == (goal.id, 1)
            assert observations[0][8] == goal.revision
    finally:
        await agent.shutdown()


def _crash_recording(root, observation, after_commit):
    store = GoalAttemptStore(root)

    def crash(*args):
        os._exit(17)

    setattr(store, "_sync" if after_commit else "_commit", crash)
    store.record_failed(observation.reservation, "crashed failure", observation=observation)


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
    assert recovered.snapshot("goal").state == ("blocked" if after_commit else "reserved")
    assert len(rows(recovered, "failed_turn_observations")) == int(after_commit)
    with pytest.raises(UnresolvedAttempt):
        recovered.resume("goal", 1)


@pytest.mark.parametrize(
    "status", [ThreadStatus.STOPPED, ThreadStatus.ARCHIVED, ThreadStatus.DELETING]
)
def test_stopped_status_is_unavailable_even_with_retained_pid(bound, status):
    store, owner, _, observation = bound
    store.record_failed(observation.reservation, "failed", observation=observation)
    assert (
        read_failed_turn_projection(
            store.path, owner=blocked(owner), owner_status=status, admission=3, pause=None
        ).state
        == "unavailable"
    )
