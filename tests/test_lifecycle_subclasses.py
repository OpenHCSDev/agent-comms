"""Lifecycle successors include declared subclasses of an accepted state."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from agent_comms.coordination_tables.executions import NoPointedAttempt, NoPointedExecution
from agent_comms.reason_codes import GivenReasonCode, NoReasonCode
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
from agent_comms.attempt_recovery import MonitorEvidence
from agent_comms.coordination_tables.attempts import AttemptRecord, ReplayFact
from agent_comms.coordination_tables.executions import CurrentExecutions, ExecutionOrigin, ExecutionRecord
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


@pytest.mark.parametrize("authorized", [False, True])
def test_unknown_settles_failed_in_current_store(tmp_path, authorized):
    with ready(tmp_path / "coordination.sqlite3", origin=ExecutionOrigin.ACP) as store:
        _, fence = started(store)
        if authorized:
            store.attempts.observe_replay(
                fence, expected_pointer_revision=1, expected_replay_revision=None,
                facts=ReplayFact.NONE, replay_safe=True, side_effects_possible=False,
            )
            final = store.attempts.advance(
                fence, PromptStartingAttempt, expected_pointer_revision=1,
                backend_done=True, process_dead=True,
            ).value
            outcome = store.attempts.settle_nonpublication(
                final.fence, expected_pointer_revision=1, outcome=UnknownOutcomeAttempt(),
            ).value
        else:
            outcome = store.attempts.fail_unknown(fence, expected_pointer_revision=1).value
        assert type(outcome.attempt.lifecycle) is UnknownOutcomeAttempt
        assert type(outcome.execution.lifecycle) is FailedExecution
        assert outcome.replay.facts & ReplayFact.UNKNOWN_EFFECTS
        assert not outcome.can_retry


@pytest.mark.parametrize("outcome, contribution", [
    (AttemptFailedAttempt(), ReplayFact.NONE),
    (UnknownOutcomeAttempt(), ReplayFact.UNKNOWN_EFFECTS),
])
def test_monitor_evidence_preserves_its_outcome_and_owned_replay_contribution(outcome, contribution):
    evidence = MonitorEvidence(
        subprocess_dead=True, backend_done=True, reason_code=GivenReasonCode("observed_exit"),
        observed_at_ms=1000, outcome=outcome,
    )
    assert evidence.outcome is outcome
    assert evidence.outcome.replay_contribution == contribution


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
        with store.session.writing() as db:
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
                execution_id=NoPointedExecution(), attempt_ordinal=NoPointedAttempt(), pointer_revision=snapshot.pointer_revision + 1,
            )
        store.participants.advance_generation("owner", "thread", expected_generation=1)
        next_attempt = replace(
            attempt, attempt_ordinal=2, owner_generation=2,
            owner_token_digest=prepare_fence_token(), lifecycle=PromptStartingAttempt(1000),
            revision=1, last_progress_at_ms=None, reason_code=NoReasonCode(),
        )
        if predecessor is UnknownOutcomeAttempt:
            with pytest.raises(sqlite3.IntegrityError):
                with store.session.writing() as db:
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


@pytest.mark.parametrize("outcome, allowed", [
    (AttemptFailedAttempt(), True),
    (UnknownOutcomeAttempt(), False),
])
def test_python_retry_permission_matches_the_outcome(outcome, allowed):
    assert outcome.permits_retry() is allowed
    assert DeferredExecution(1).can_retry(
        authorized=True, attempt=outcome, is_current=False,
    ) is allowed
