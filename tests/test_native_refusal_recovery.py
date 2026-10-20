"""Native refusal recovery: Core reports one outcome, never blocks on a retry."""
from agent_comms.native_arguments import SelectedModel

import json

import pytest

import agent_comms.agent_events as ae
import agent_comms.native_pi as native_pi
import agent_comms.pi_events as pi_events
from pi_session_turn import one_turn_events
from test_backend import _NATIVE_PROMPT_START, _replay_stub

OVERLOADED = "Our servers are currently overloaded. Please try again later."

pytestmark = pytest.mark.usefixtures("native_rpc_fixture")


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


@pytest.mark.parametrize("reason", ["error", "length", "aborted"])
async def test_explicit_failed_terminal_is_not_a_missing_final_stop(tmp_path, reason):
    records = [
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {"type": "message_end", "message": {
            "role": "assistant", "stopReason": reason,
            "errorMessage": "terminated", "content": [],
        }},
        *_settled_records(),
    ]
    stub = _stub_with(tmp_path, records)
    observed = [event async for event in one_turn_events(
        stub, [], "distinct terminal mapping input", str(tmp_path),
    )]
    done = observed[-1]
    assert isinstance(done, ae.Done) and not done.ok
    assert done.reason_code == "model_request_failed"
    assert done.text == "terminated"
    assert len([event for event in observed if isinstance(event, ae.Done)]) == 1
    assert [event.text for event in observed if isinstance(event, ae.Error)] == ["terminated"]


async def test_failed_native_retry_end_without_assistant_still_declares_failure(tmp_path):
    stub = _stub_with(tmp_path, [
        {"type": "native_retry_end", "success": False, "attempt": 3,
         "finalError": "terminated before an assistant terminal"},
        *_settled_records(),
    ])
    observed = [event async for event in one_turn_events(
        stub, [], "distinct retry-end-only input", str(tmp_path),
    )]
    done = observed[-1]
    assert isinstance(done, ae.Done) and not done.ok
    assert done.reason_code == "model_request_failed"
    assert done.text == "terminated before an assistant terminal"
    assert [event.text for event in observed if isinstance(event, ae.Error)] == [done.text]
    assert len([event for event in observed if isinstance(event, ae.Done)]) == 1


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


@pytest.mark.asyncio
async def test_exhausted_native_retries_report_the_existing_failure_once(tmp_path):
    records = [
        *_refused_attempt(1, 1_000),
        *_refused_attempt(2, 10_000),
        *_refused_attempt(3, 60_000),
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {"type": "native_retry_end", "success": False, "attempt": 3,
         "finalError": OVERLOADED},
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
         "errorMessage": OVERLOADED, "content": []}},
        *_settled_records(),
    ]
    stub = _stub_with(tmp_path, records)
    events = [
        event
        async for event in one_turn_events(
            stub, [], "task", str(tmp_path)
        )
    ]

    states = [event for event in events if isinstance(event, ae.TurnState)]
    assert [event.state for event in states] == [
        "retrying", "retrying", "retrying", "failed",
    ]
    failed = states[-1]
    assert failed.reason_code == "provider_retry_exhausted"
    # The retained final refusal is the one failure report, with native provenance.
    errors = [event for event in events if isinstance(event, ae.Error)]
    assert [event.text for event in errors] == [OVERLOADED]
    done = events[-1]
    assert isinstance(done, ae.Done) and done.ok is False and done.text == OVERLOADED
