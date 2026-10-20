"""Exact terminal claim release on real private stores and worker processes."""

from bus_history import read_history
import asyncio
import json
import os
import shutil
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_comms.attempt_states import AttemptFailedAttempt, UnknownOutcomeAttempt
from agent_comms.selected_claim import publish_selected_resource_claim
from agent_comms.coordination_tables.attempts import ReplayFact
from agent_comms.envelope_claim_transitions import ExistingFileClaim
from agent_comms.execution_states import FailedExecution
from agent_comms.obligation_states import FailedResponse
from agent_comms.owner_fence import OwnerFence
from test_claim_bus_read import acquire_tool_claim, claim_originals, selected_turn


def test_unknown_outcome_never_defers_when_retry_is_otherwise_authorized():
    execution, response = UnknownOutcomeAttempt().disposition(
        SimpleNamespace(retry_authorized=True), SimpleNamespace(attempt_ordinal=1)
    )

    assert type(execution) is FailedExecution
    assert type(response) is FailedResponse


def test_terminal_release_spans_the_existing_envelope_bound():
    with selected_turn() as (comms, store, selected, resource):
        for index in range(35):
            file = resource.parent / f"file-{index}.py"
            file.write_text("original\n")
            publish_selected_resource_claim(
                comms, store, replace(selected, operation_id=f"{index:032x}"), "Alice", ExistingFileClaim(file)
            )
        from test_claim_bus_read import settle_failed
        settle_failed(store)
        assert dict(comms.bus.log.claim_projection()) == {}
        notices = [m for m in claim_originals(comms) if m.claim_transition.releases]
        releases = [m.claim_transition for m in notices]
        assert [len(row.releases) for row in releases] == [32, 3]
        assert all(m.body.startswith("Released files after a failed turn: ") for m in notices)
        assert sum(m.body.count("file-") for m in notices) == 35


