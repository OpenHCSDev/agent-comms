"""The observed empty native1011 response retains typed stage without send inference."""

import copy
import json
from pathlib import Path

import pytest

from agent_comms.acp_extension import RequestFailedUpdate, decode_updates, encode_updates
from agent_comms.acp_failure import ACPFailure, ProviderConnectionFailure, RequestACPFailure
from agent_comms.input_attempt import StartedInput
from agent_comms.pi_events import MessageEnd
from agent_comms.pi_payloads import AfterMessageStreamStartStage, UnrecognizedTransportStage
from agent_comms.pi_rpc import PiRpcChannel

FIXTURE = Path(__file__).with_name("fixtures") / "native_provider_transport_1011.json"


def observed():
    return json.loads(FIXTURE.read_text())


def test_observed_native_failure_preserves_stage_and_no_content_assumptions():
    event = PiRpcChannel.decode_record(FIXTURE.read_bytes(), strict=True)
    assert isinstance(event, MessageEnd)
    assert event.message.content == ()
    assert event.message.stop_reason == "error"
    assert event.message.usage.total_tokens == 0
    (diagnostic,) = event.message.diagnostics
    assert diagnostic.error.name == "WebSocketCloseError"
    assert diagnostic.error.code == 1011
    assert diagnostic.details.request_bytes == 159312
    assert isinstance(diagnostic.details.phase, AfterMessageStreamStartStage)
    failure = ACPFailure.from_error(
        -32603, event.message.error_message, diagnostics=event.message.diagnostics
    )
    assert isinstance(failure, ProviderConnectionFailure)
    assert "code 1011" in failure.description
    assert "after the provider response stream started" in failure.description
    assert "configured transport: auto" in failure.description
    assert "provider events emitted: yes" in failure.description
    assert failure.input_disposition == "Unconfirmed — input not retried"
    assert decode_updates(encode_updates(RequestFailedUpdate(failure)))[0].failure == failure


def test_structured_started_remains_started_and_no_text_changes_it():
    record = observed()["message"]
    failure = ACPFailure.from_error(
        -32603,
        record["errorMessage"],
        {
            "inputStatus": "started",
            "diagnostics": record["diagnostics"],
        },
    )
    assert isinstance(failure, ProviderConnectionFailure)
    assert failure.input_state is StartedInput
    assert failure.input_disposition == "Started — input not retried"
    bare = ACPFailure.from_error(
        -32603, "WebSocket closed1011 after start; original remains unbound"
    )
    assert isinstance(bare, RequestACPFailure)
    assert bare.input_state is None
    assert bare.input_disposition == "Unconfirmed — input not retried"


@pytest.mark.parametrize(
    "field,value", [("eventsEmitted", 1), ("requestBytes", True), ("requestBytes", -1)]
)
def test_malformed_native_diagnostic_is_rejected_at_existing_boundary(field, value):
    record = observed()
    record["message"]["diagnostics"][0]["details"][field] = value
    with pytest.raises(ValueError):
        PiRpcChannel.decode_record((json.dumps(record) + "\n").encode(), strict=True)


def test_unrecognized_phase_is_a_typed_observation_not_a_send_decision():
    record = copy.deepcopy(observed())
    record["message"]["diagnostics"][0]["details"]["phase"] = "future_native_stage"
    event = PiRpcChannel.decode_record((json.dumps(record) + "\n").encode(), strict=True)
    (diagnostic,) = event.message.diagnostics
    assert isinstance(diagnostic.details.phase, UnrecognizedTransportStage)
    failure = ACPFailure.from_error(
        -32603, event.message.error_message, diagnostics=event.message.diagnostics
    )
    assert "provider stage: future_native_stage" in failure.description
    assert failure.input_state is None
