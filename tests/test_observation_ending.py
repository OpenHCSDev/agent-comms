"""Ending an observation is the process's own outcome, not an error for its callers.

A peer that already dropped the connection makes `request` raise its terminal outcome. The
caller ending the observation is entering that state, so the goodbye must not raise it — and
must not swallow anything else.
"""

import multiprocessing

import pytest

from agent_comms.ui_model.observation import (
    ObservationEnded,
    ObservationProcess,
    ObserverDied,
    ObserverRetired,
    StopObserving,
)


class Peer:
    def __init__(self, connection):
        self.connection = connection

    def retire(self):
        pass


class Source:
    def __init__(self, connection):
        self.connection = connection

    def observation_peer(self):
        return Peer(self.connection)


def process_with_closed_connection():
    mine, theirs = multiprocessing.Pipe()
    process = ObservationProcess(Source(mine))
    mine.close()
    theirs.close()
    return process


def test_the_terminal_outcomes_share_one_base():
    assert issubclass(ObserverRetired, ObservationEnded)
    assert issubclass(ObserverDied, ObservationEnded)


def test_a_goodbye_to_a_closed_peer_does_not_raise():
    process = process_with_closed_connection()
    with pytest.raises(ObservationEnded):
        process.request(StopObserving())
    process.stop_observing()


def test_an_unrelated_failure_still_propagates():
    process = process_with_closed_connection()

    def broken(request):
        raise ValueError("not an observation outcome")

    process.request = broken
    with pytest.raises(ValueError):
        process.stop_observing()
