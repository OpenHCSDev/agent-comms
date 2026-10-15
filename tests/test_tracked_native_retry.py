import pytest

from agent_comms.pi_events import NativeRetryEnd
from agent_comms.pi_payloads import FailedProviderRetry, SucceededProviderRetry
from agent_comms.tracked_turn import (
    PendingTrackedTerminal,
    TrackedTurnSession,
)
from agent_comms.turn_output import TurnOutput
from agent_comms.turn_failure import ModelRequestFailed, ProviderRetryExhausted
from agent_comms.native_pi import NativePiTerminalFailure


async def test_recovered_native_request_replaces_prior_refusal_terminal():
    turn = object.__new__(TrackedTurnSession)
    turn.output = TurnOutput(failure=ModelRequestFailed("provider refusal"))
    turn.terminal = PendingTrackedTerminal()

    event = NativeRetryEnd.from_wire({
        "type": "native_retry_end", "success": True, "attempt": 1
    })
    assert isinstance(event.outcome, SucceededProviderRetry)
    assert event.to_wire() == {
        "type": "native_retry_end", "success": True, "attempt": 1
    }
    await turn.native_retry_finished(event)

    assert turn.output.failure is None
    assert turn.output.retry_result.success
    assert isinstance(turn.terminal, PendingTrackedTerminal)


@pytest.mark.parametrize("previous", [None, ModelRequestFailed("prior refusal")])
async def test_exhausted_native_request_records_required_final_error(previous):
    turn = object.__new__(TrackedTurnSession)
    turn.output = TurnOutput(failure=previous)
    turn.terminal = PendingTrackedTerminal()
    event = NativeRetryEnd.from_wire({
        "type": "native_retry_end",
        "success": False,
        "attempt": 3,
        "finalError": "final refusal",
    })

    assert isinstance(event.outcome, FailedProviderRetry)
    assert event.to_wire() == {
        "type": "native_retry_end",
        "success": False,
        "attempt": 3,
        "finalError": "final refusal",
    }
    await turn.native_retry_finished(event)

    assert turn.output.declared_failure == ProviderRetryExhausted("final refusal")
    assert turn.output.retry_result.attempt == 3
    with pytest.raises(NativePiTerminalFailure, match="final refusal") as caught:
        turn.output.raise_if_failed(None, "configured", "model")
    assert caught.value.failure == turn.output.declared_failure


def test_failed_native_retry_end_requires_final_error_at_decode():
    with pytest.raises(ValueError, match="requires finalError"):
        NativeRetryEnd.from_wire({
            "type": "native_retry_end", "success": False, "attempt": 3
        })
