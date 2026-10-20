"""The append owner schedules optional work after the whole lock lifetime."""

from bus_history import read_history
from conftest import publish_initial
from contextvars import Context
from functools import partial
from tempfile import TemporaryDirectory
import sqlite3
import threading

import pytest

from agent_comms.bus_publication import AgentOrigin
import agent_comms.candidate_maintenance as candidate_maintenance
import agent_comms.selected_claim as selected_claim
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_response import (
    prepare_fenced_response, publish_fenced_response, resolve_existing_response,
)
from agent_comms.context_segments.optional_awareness import OmittedAwareness
from agent_comms.envelope_claim_transitions import ExistingFileClaim
from agent_comms.errors import RelationViolationError
from agent_comms.messages import InfoKind, Message
from agent_comms.store_files import StoreLock, _async_store_lock, _store_lock
from agent_comms.wake_candidate_index import (
    WakeCandidateIndex, RebuildIndex,
)
from test_claim_bus_read import record_context, selected_turn
from test_coordination_response import _ready
from test_optional_awareness_projection import _root, _accepted, _owner, _projection

_SCHEDULE = candidate_maintenance.schedule_candidate_catchup


def test_release_scope_closes_after_context_switch_and_stale_context_starts_fresh():
    calls = []
    entered, exited = Context(), Context()
    release = StoreLock.releasing()

    entered.run(release.__enter__)
    entered.run(lambda: StoreLock.current_release().callback(calls.append, "first"))
    exited.run(release.__exit__, None, None, None)
    assert calls == ["first"]

    def independent():
        with StoreLock.releasing():
            StoreLock.current_release().callback(calls.append, "second")
            assert calls == ["first"]

    entered.run(independent)
    assert calls == ["first", "second"]


def test_nested_release_scope_runs_only_after_outer_release():
    calls = []
    with StoreLock.releasing() as outer:
        with StoreLock.releasing() as inner:
            assert inner is outer
            StoreLock.current_release().callback(calls.append, "released")
        assert calls == []
    assert calls == ["released"]


