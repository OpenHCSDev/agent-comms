"""An unreadable triage decision costs the turn, not the claim."""
from pathlib import Path

import pytest

from agent_comms.coordination.coordinator import Coordination
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.selected_triage import InvalidTriageDecision, RejectedTriageOutcome
from test_coordinated_runtime import tmp_path  # noqa: F401
from test_native_failure_recovery import prepared_native_triage
from test_live_context_notifications import recorded_input_context, acknowledge_context


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
