"""Native refusal recovery: Core reports one outcome, never blocks on a retry."""
from agent_comms.native_arguments import SelectedModel

import json

import pytest

import agent_comms.native_pi as native_pi
import agent_comms.pi_events as pi_events
from test_backend import _NATIVE_PROMPT_START, _replay_stub

OVERLOADED = "Our servers are currently overloaded. Please try again later."



def _stub_with(tmp_path, records):
    lines = "\n".join(json.dumps(record) for record in records)
    return _replay_stub(
        tmp_path,
        lines,
        startup="def emit(value):\n    print(json.dumps(value), flush=True)\n"
        + _NATIVE_PROMPT_START
        + "prompt_request = prompt\n",
    )


def _refused_attempt(attempt, delay_ms):
    """The recovery announces its retry decision before the refused message_end."""
    return [
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {"type": "native_retry_start", "attempt": attempt, "maxAttempts": 3,
         "delayMs": delay_ms, "errorMessage": OVERLOADED},
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
         "errorMessage": OVERLOADED, "content": []}},
    ]


def _settled_records():
    return [
        {"type": "agent_settled"},
        {"type": "response", "command": "get_session_stats", "success": True,
         "data": {"contextUsage": {}}},
    ]






@pytest.mark.asyncio
async def test_exhausted_provider_retries_become_typed_provider_failure():
    reason = "503 overloaded after native provider retries"
    from agent_comms.turn_output import TurnOutput

    from agent_comms.turn_failure import ModelRequestFailed, ProviderRetryExhausted

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


