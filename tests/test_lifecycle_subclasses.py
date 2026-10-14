"""Lifecycle successors include declared subclasses of an accepted state."""

from __future__ import annotations

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
)


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
    assert source.successors().count(AttemptFailedAttempt) == 1


def test_attempt_transition_table_expands_declared_failure_subclasses():
    transitions = AttemptState.transition_table()

    for source in FAILED_SUCCESSOR_SOURCES:
        assert {AttemptFailedAttempt.declared_name, UnknownOutcomeAttempt.declared_name} <= transitions[
            source.declared_name
        ]
