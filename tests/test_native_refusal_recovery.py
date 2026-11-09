"""Native refusal recovery: Core reports one outcome, never blocks on a retry."""
import pytest

import agent_comms.execution.native_pi as native_pi
import agent_comms.execution.pi_events as pi_events
from agent_comms.execution.native_arguments import SelectedModel


@pytest.mark.asyncio
async def test_exhausted_provider_retries_become_typed_provider_failure():
    reason = "503 overloaded after native provider retries"
    from agent_comms.execution.turn_failure import (
        ModelRequestFailed,
        ProviderRetryExhausted,
    )
    from agent_comms.execution.turn_output import TurnOutput

    output = TurnOutput()
    event = pi_events.NativeRetryEnd.from_wire({
        "type": "native_retry_end",
        "success": False,
        "attempt": 3,
        "finalError": reason,
    })
    output.retry_result = event.outcome.result(event.attempt, output.redact)
    # Retry metadata alone is not a failed assistant terminal. Its existing
    # output owner refines the actual native terminal when that arrives.
    output.record_failure(ModelRequestFailed(reason))
    with pytest.raises(native_pi.NativePiTerminalFailure) as caught:
        output.raise_if_failed(None, SelectedModel("provider", "model"))
    assert reason in str(caught.value)
    assert caught.value.failure == ProviderRetryExhausted(reason)


