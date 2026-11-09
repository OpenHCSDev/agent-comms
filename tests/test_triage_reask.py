"""An unreadable triage decision costs the turn, not the claim."""
from pathlib import Path

import pytest

from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.native_runtime_input import NativeRuntimeInput
from agent_comms.execution.selected.triage import (
    InvalidTriageDecision,
    RejectedTriageOutcome,
)
from test_coordinated_runtime import tmp_path  # noqa: F401
from test_live_context_notifications import acknowledge_context, recorded_input_context
from test_native_failure_recovery import prepared_native_triage


def refusal() -> str:
    return RejectedTriageOutcome(InvalidTriageDecision("bad"), "nonsense").reason()


async def test_an_unreadable_decision_leaves_the_claim_deferred(tmp_path: Path):
    async with prepared_native_triage(tmp_path) as claim:
        context = await recorded_input_context(claim)
        await acknowledge_context(claim, context)
        captured = tuple(claim.stage.assignments)
        with Coordination(claim.store_path) as store:
            claim.stage.burn_turn(store, claim.participant, claim.input_id,
                                  claim.token_digest, context)
            # The claims are not failed: the reader still owes a decision.
            failed = "FailedAssignment"
            for original in captured:
                current = store.assignments.get(original.assignment_id)
                assert type(current.lifecycle).__name__ != failed, (
                    f"an unreadable decision failed the claim: {current.lifecycle}"
                )
            retained = NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id)
            assert retained.require_context_proof() == context


async def test_one_claim_takes_an_original_ask_and_one_replacement(tmp_path: Path):
    async with prepared_native_triage(tmp_path) as claim:
        context = await recorded_input_context(claim)
        await acknowledge_context(claim, context)
        with Coordination(claim.store_path) as store:
            # The unreadable answer is closed first; that is what returns the claim to
            # triage-pending and makes one replacement legal.
            claim.stage.burn_turn(store, claim.participant, claim.input_id,
                                  claim.token_digest, context)
            stage = claim.stage.refreshed(store)
            replacement = stage.reserve(store, claim.participant, claim.token_digest, attempt=1)
            assert replacement != claim.input_id
            assert len(NativeRuntimeInput.select(store.session._connection)) == 2
            with pytest.raises(Exception):
                stage.refreshed(store).reserve(store, claim.participant,
                                               claim.token_digest, attempt=1)


async def test_the_replacement_alone_proves_the_source(tmp_path: Path):
    from agent_comms.execution.native_input_record import TriageNativeExecution

    async with prepared_native_triage(tmp_path) as claim:
        context = await recorded_input_context(claim)
        await acknowledge_context(claim, context)
        with Coordination(claim.store_path) as store:
            claim.stage.burn_turn(store, claim.participant, claim.input_id,
                                  claim.token_digest, context)
            db = store.session._connection
            # Between the burn and the replacement's reservation the claim is triage-pending
            # again; an inbox read in that window must not take the burned input as proof.
            assert TriageNativeExecution().retried_in(db, claim.input_id)
            replacement = claim.stage.refreshed(store).reserve(
                store, claim.participant, claim.token_digest, attempt=1)
            # Historical evidence keeps one triage input per source: the burned original is
            # superseded, as a FULL attempt behind a later one is.
            assert TriageNativeExecution().retried_in(db, claim.input_id)
            assert not TriageNativeExecution().retried_in(db, replacement)
