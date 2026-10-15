"""Lifecycle successors include declared subclasses of an accepted state."""

from __future__ import annotations

import sqlite3

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
from agent_comms.coordination_tables.attempts import AttemptRecord
from agent_comms.coordinator import StandaloneCoordination


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
        db.execute("CREATE TRIGGER retired_attempt_trigger BEFORE INSERT ON attempts "
                   "BEGIN SELECT 1; END")

    with StandaloneCoordination(path) as store:
        db = store.session._connection
        actual = db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'attempt_insert_authorized'"
        ).fetchone()[0]
        assert actual == AttemptRecord.schema_objects()["attempt_insert_authorized"]
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'retired_attempt_trigger'"
        ).fetchone() is None
        assert db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall() == tables_before
        assert store.session._trigger_differences() == ()
