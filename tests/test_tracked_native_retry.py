import pytest

from agent_comms.turn_output import TurnOutput
from agent_comms.turn_failure import ModelRequestFailed, ProviderRetryExhausted

from agent_comms.pi_events import NativeRetryEnd
from agent_comms.pi_payloads import FailedProviderRetry, SucceededProviderRetry
from agent_comms.tracked_turn import (
    FailedTrackedTerminal,
    PendingTrackedTerminal,
    TrackedTurnSession,
)


async def test_recovered_native_request_replaces_prior_refusal_terminal():
    turn = object.__new__(TrackedTurnSession)
    turn.output = TurnOutput()
    turn.output.record_failure(ModelRequestFailed("provider refusal"))
    turn.terminal = FailedTrackedTerminal("provider refusal")

    event = NativeRetryEnd.from_wire({
        "type": "native_retry_end", "success": True, "attempt": 1
    })
    assert isinstance(event.outcome, SucceededProviderRetry)
    assert event.to_wire() == {
        "type": "native_retry_end", "success": True, "attempt": 1
    }
    await turn.native_retry_finished(event)

    assert turn.output.failure is None
    assert isinstance(turn.terminal, PendingTrackedTerminal)


async def test_exhausted_native_request_records_required_final_error():
    turn = object.__new__(TrackedTurnSession)
    turn.output = TurnOutput()
    turn.terminal = FailedTrackedTerminal("prior refusal")
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

    assert turn.terminal == FailedTrackedTerminal("final refusal")
    assert turn.output.declared_failure == ProviderRetryExhausted("final refusal")


def test_failed_native_retry_end_requires_final_error_at_decode():
    with pytest.raises(ValueError, match="requires finalError"):
        NativeRetryEnd.from_wire({
            "type": "native_retry_end", "success": False, "attempt": 3
        })
