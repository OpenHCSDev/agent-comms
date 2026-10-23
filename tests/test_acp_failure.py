"""Shared failure presentation preserves reasons, input and native retry facts."""

import json

import pytest
from acp import RequestError

from agent_comms.acp_extension import RequestFailedUpdate, decode_updates, encode_updates
from agent_comms.acp_failure import (
    ACPFailure, RequestFailure, ProviderQuotaFailure, ProviderConnectionFailure, RequestACPFailure,
)
from agent_comms.input_attempt import InputAttempt


@pytest.mark.parametrize("wrap", [
    lambda text: {"details": text},
    lambda text: {"error": {"message": "Internal error", "data": {"reason": text}}},
    lambda text: {"error": json.dumps({"message": "Internal error", "data": {"details": text}})},
])
def test_provider_reason_survives_external_nesting(wrap):
    reason = "Selected summary failed: Codex error: The usage limit has been reached"
    failure = ACPFailure.from_error(RequestError(-32603, "Internal error", wrap(reason)))
    assert isinstance(failure, ProviderQuotaFailure)
    assert failure.detail == reason
    assert failure.input_disposition == "Unconfirmed"
    assert failure.action in failure.feedback
    fact = RequestFailedUpdate(failure)
    assert decode_updates(encode_updates(fact)) == (fact,)


@pytest.mark.parametrize("state", InputAttempt.members_with(InputAttempt))
def test_disposition_derives_from_shared_input_owner(state):
    failure = ACPFailure.from_error(RequestError(-32603, "Internal error", {
        "reason": "Provider unavailable", "inputStatus": state.declared_name,
    }))
    assert isinstance(failure, RequestACPFailure)
    assert failure.input_state is state
    assert failure.input_disposition == state.public_status.replace("_", " ").capitalize()
    assert decode_updates(encode_updates(RequestFailedUpdate(failure)))[0].failure == failure


def test_public_not_sent_and_ambiguous_unknown_do_not_invent_binding():
    from agent_comms.input_attempt import NotSentInput
    failure = ACPFailure.from_error(RequestError(-32603, "Preflight failed", {"inputStatus": "not_sent"}))
    assert failure.input_state is NotSentInput
    assert failure.input_disposition == "Not sent"
    unknown = ACPFailure.from_error(RequestError(-32603, "Outcome uncertain", {"inputStatus": "unknown"}))
    assert unknown.input_state is None and unknown.input_disposition == "Unconfirmed"


def test_unmatched_display_detail_reaches_the_last_declared_fallback():
    failure = ACPFailure.from_error(RequestError(-32603, "T2 unmatched display detail"))
    assert isinstance(failure, RequestACPFailure)
    assert failure.input_disposition == "Unconfirmed"
    assert decode_updates(encode_updates(RequestFailedUpdate(failure)))[0].failure == failure


@pytest.mark.parametrize("payload", [
    "The provider usage limit has been reached",
    [None, 400, {"error": {"details": "The provider usage limit has been reached"}}],
    {"error": {"data": [False, {"reason": "The provider usage limit has been reached"}]}},
    {"reason": json.dumps([{"details": "The provider usage limit has been reached"}])},
])
def test_provider_shapes_preserve_reason_without_granting_input_authority(payload):
    failure = ACPFailure.from_error(RequestError(-32603, "Internal error", payload))
    assert isinstance(failure, ProviderQuotaFailure)
    assert failure.detail == "The provider usage limit has been reached"
    assert failure.input_disposition == "Unconfirmed"


def test_only_original_root_metadata_owns_disposition_and_reported_failure():
    from agent_comms.input_attempt import NotSentInput
    nested = {"error": {"reason": "Provider refused", "inputStatus": "not_sent"}}
    assert ACPFailure.from_error(RequestError(-32603, "Internal error", nested)).input_state is None
    original = RequestFailure(RequestACPFailure(-32603, "Original preflight refusal", NotSentInput), True)
    data = {**original.error_data(), "error": {"reason": "Unrelated nested provider detail"}}
    assert RequestFailure.from_error(RequestError(-32603, "Internal error", data)) == original


@pytest.mark.parametrize("payload", [None, False, 37, [], {}, {"details": "Internal error"}])
def test_empty_or_nontext_provider_payload_retains_rpc_message(payload):
    failure = ACPFailure.from_error(RequestError(-32603, "Original RPC refusal", payload))
    assert failure.detail == "Original RPC refusal" and failure.input_disposition == "Unconfirmed"


def test_encoded_provider_text_cannot_supply_original_root_disposition():
    data = json.dumps({"reason": "Provider refused", "inputStatus": "not_sent"})
    failure = ACPFailure.from_error(RequestError(-32603, "Internal error", data))
    assert "Provider refused" in failure.detail and failure.input_state is None


def test_reported_failure_detail_is_not_reinterpreted_as_provider_json():
    from agent_comms.input_attempt import StartedInput
    detail = '{"message" : "Internal error", "details" : [ "verbatim reported failure" ]}'
    request_failure = RequestFailure(RequestACPFailure(-32603, detail, StartedInput), True)
    assert RequestFailure.from_error(RequestError(-32603, "Internal error", request_failure.error_data())) == request_failure


def test_provider_array_order_and_unknown_json_text_survive():
    payload = [{"details": "First provider reason"}, {"details": "Second provider reason"}]
    assert ACPFailure.from_error(RequestError(-32603, "Internal error", payload)).detail == "First provider reason"
    unknown = '{"vendorDiagnostic" : {"future" : 17}}'
    assert ACPFailure.from_error(RequestError(-32603, "RPC refused", unknown)).detail == unknown


def test_declared_provider_retry_exhaustion_owns_execution_retry_classification():
    from agent_comms.acp_failure import ExhaustedRetry
    from agent_comms.turn_failure import ModelRequestFailed, ProviderRetryExhausted
    from agent_comms.turn_output import TurnOutput

    ordinary = TurnOutput(failure=ModelRequestFailed("quota"))
    assert type(ordinary.declared_failure) is ModelRequestFailed

    transient = TurnOutput(
        failure=ModelRequestFailed("opaque provider failure"),
        retry_result=ExhaustedRetry(attempt=3, final_error="opaque provider failure"),
    )
    assert type(transient.declared_failure) is ProviderRetryExhausted

    from agent_comms.pi_payloads import PiDiagnostic
    from agent_comms.turn_failure import ProviderTransportFailed

    transport = PiDiagnostic.from_wire({
        "type": "provider_transport_failure",
        "error": {"name": "WebSocketClose", "message": "closed", "code": "1000"},
        "details": {
            "configuredTransport": "websocket",
            "phase": "before_message_stream_start",
            "eventsEmitted": False,
            "requestBytes": 123,
        },
    })
    classified = ModelRequestFailed.from_diagnostics(
        "opaque transport close", (transport,)
    )
    assert type(classified) is ProviderTransportFailed


@pytest.mark.parametrize("failure_type", [RequestACPFailure, ProviderConnectionFailure, ProviderQuotaFailure])
def test_every_failure_action_uses_native_retry_end_not_text_claims(failure_type):
    from agent_comms.pi_events import NativeRetryEnd
    from agent_comms.field_codec import FieldCodec
    event = NativeRetryEnd.from_wire({
        "type": "native_retry_end",
        "success": False,
        "attempt": 3,
        "finalError": "429 provider rate limit",
    })
    result = event.outcome.result(event.attempt, lambda text: text)
    failure = failure_type(-32603, "final error", retry_result=result)
    assert "Provider request retries: 3." in failure.action
    assert "429 provider rate limit" in failure.action
    assert "not retried" not in failure.feedback
    reported = RequestFailure(failure, True)
    assert FieldCodec.decode(RequestFailure, FieldCodec.encode(reported)) == reported
    exhausted = ACPFailure.from_error(RequestError(-32603, "429 rate limit"))
    assert type(exhausted) is ProviderQuotaFailure
    assert exhausted.title == "Provider usage limit reached"
    assert exhausted.next_step.startswith("Wait for the provider limit")
