"""Lifecycle successors include declared subclasses of an accepted state."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from agent_comms.attempt_states import (
    AbortingAttempt,
    AttemptFailedAttempt,
    AttemptState,
    CompactionAttempt,
    ModelRunningAttempt,
    ModelStalledAttempt,
    PromptAcceptedAttempt,
    PromptStartingAttempt,
    ProviderUnavailableAttempt,
    RetryingAttempt,
    SettlingAttempt,
    ToolRunningAttempt,
    UnknownOutcomeAttempt,
    UnsuccessfulTerminal,
)
from agent_comms.coordination_tables.attempts import AttemptRecord, ReplayFact
from agent_comms.coordination_tables.executions import CurrentExecutions, ExecutionOrigin, ExecutionRecord
from agent_comms.coordinator import StandaloneCoordination
from agent_comms.execution_states import DeferredExecution, FailedExecution
from agent_comms.owner_fence import prepare_fence_token
from test_coordination_store import final_evidence, ready, started


FAILED_SUCCESSOR_SOURCES = (
    PromptStartingAttempt,
    PromptAcceptedAttempt,
    ModelRunningAttempt,
    ToolRunningAttempt,
    CompactionAttempt,
    SettlingAttempt,
    ModelStalledAttempt,
    AbortingAttempt,
    RetryingAttempt,
    ProviderUnavailableAttempt,
)


@pytest.mark.parametrize("source", FAILED_SUCCESSOR_SOURCES)
def test_attempt_failure_successor_accepts_declared_unknown_subclass(source):
    assert source(1).may_become(UnknownOutcomeAttempt())
    assert source.successors().count(UnsuccessfulTerminal) == 1


def test_attempt_transition_table_expands_declared_failure_subclasses():
    transitions = AttemptState.transition_table()

    for source in FAILED_SUCCESSOR_SOURCES:
        assert {AttemptFailedAttempt.declared_name, UnknownOutcomeAttempt.declared_name} <= transitions[
            source.declared_name
        ]


def test_unknown_settles_failed_in_current_store(tmp_path):
    with ready(tmp_path / "coordination.sqlite3", origin=ExecutionOrigin.ACP) as store:
        _, fence = started(store)
        outcome = store.attempts.fail_unknown(fence, expected_pointer_revision=1).value
        assert type(outcome.attempt.lifecycle) is UnknownOutcomeAttempt
        assert type(outcome.execution.lifecycle) is FailedExecution
        assert not outcome.can_retry


@pytest.mark.parametrize("predecessor", [AttemptFailedAttempt, UnknownOutcomeAttempt])
def test_sql_retry_predecessor_is_not_every_unsuccessful_outcome(tmp_path, predecessor):
    with ready(tmp_path / "coordination.sqlite3", origin=ExecutionOrigin.ACP) as store:
        _, fence = started(store)
        store.attempts.observe_replay(
            fence, expected_pointer_revision=1, expected_replay_revision=None,
            facts=ReplayFact.NONE, replay_safe=True, side_effects_possible=False,
        )
        final_evidence(store, fence)
        snapshot = store.snapshots.get("exec")
        attempt = snapshot.require_attempt()
        with store.session.transaction() as db:
            # Deliberately preserve otherwise-valid retry authority to verify
            # SQL rejects UNKNOWN independently of Python settlement policy.
            AttemptRecord.update(
                db, where="execution_id=? AND attempt_ordinal=?", parameters=("exec", 1),
                lifecycle=predecessor(), revision=attempt.revision + 1,
            )
            ExecutionRecord.update(
                db, where="execution_id=?", parameters=("exec",),
                lifecycle=DeferredExecution(1), revision=snapshot.execution.revision + 1,
            )
            CurrentExecutions.update(
                db, where="owner_lookup=?", parameters=("owner",),
                execution_id=None, attempt_ordinal=None, pointer_revision=snapshot.pointer_revision + 1,
            )
        store.participants.advance_generation("owner", "thread", expected_generation=1)
        next_attempt = replace(
            attempt, attempt_ordinal=2, owner_generation=2,
            owner_token_digest=prepare_fence_token(), lifecycle=PromptStartingAttempt(1000),
            revision=1, last_progress_at_ms=None, reason_code=None,
        )
        if predecessor is UnknownOutcomeAttempt:
            with pytest.raises(sqlite3.IntegrityError, match="previous attempt must be dead"):
                with store.session.transaction() as db:
                    next_attempt.insert(db)
        else:
            # The ordinary failed predecessor still reaches the insert. Roll
            # back because this check intentionally does not activate attempt 2.
            db = store.session._connection
            db.execute("BEGIN IMMEDIATE")
            try:
                next_attempt.insert(db)
            finally:
                db.execute("ROLLBACK")


def test_open_replaces_stale_trigger_and_removes_retired_trigger(tmp_path):
    path = tmp_path / "coordination.sqlite3"
    with StandaloneCoordination(path) as store:
        db = store.session._connection
        tables_before = db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        db.execute("DROP TRIGGER attempt_insert_authorized")
        db.execute("CREATE TRIGGER attempt_insert_authorized BEFORE INSERT ON attempts "
                   "BEGIN SELECT RAISE(ABORT, 'obsolete authority'); END")
        db.execute('CREATE TRIGGER "retired attempt trigger" BEFORE INSERT ON attempts '
                   "BEGIN SELECT 1; END")

    with StandaloneCoordination(path) as store:
        db = store.session._connection
        actual = db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'attempt_insert_authorized'"
        ).fetchone()[0]
        assert actual == AttemptRecord.schema_objects()["attempt_insert_authorized"]
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'retired attempt trigger'"
        ).fetchone() is None
        assert db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall() == tables_before
        assert store.session._trigger_differences() == ()
