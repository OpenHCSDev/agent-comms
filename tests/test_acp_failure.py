"""The shared failure owner preserves external reasons and declared input states."""

import json

import pytest

from agent_comms.acp_extension import RequestFailedUpdate, decode_updates, encode_updates
from agent_comms.acp_failure import (
    ACPFailure,
    PromptFailureReceipt,
    ProviderQuotaFailure,
    RequestACPFailure,
)
from agent_comms.input_attempt import InputAttempt


@pytest.mark.parametrize(
    "wrap",
    [
        lambda text: {"details": text},
        lambda text: {"error": {"message": "Internal error", "data": {"reason": text}}},
        lambda text: {
            "error": json.dumps({"message": "Internal error", "data": {"details": text}})
        },
    ],
)
def test_provider_reason_survives_external_nesting(wrap):
    reason = "Selected summary failed: Codex error: The usage limit has been reached (outcome uncertain; input not retried)"
    failure = ACPFailure.from_error(-32603, "Internal error", wrap(reason))
    assert isinstance(failure, ProviderQuotaFailure)
    assert failure.detail == reason
    assert failure.input_disposition == "Unconfirmed — input not retried"
    assert failure.action in failure.feedback
    fact = RequestFailedUpdate(failure)
    assert decode_updates(encode_updates(fact)) == (fact,)


@pytest.mark.parametrize("state", InputAttempt.members_with(InputAttempt))
def test_disposition_derives_from_shared_input_owner(state):
    failure = ACPFailure.from_error(
        -32603,
        "Internal error",
        {"reason": "Provider unavailable", "inputStatus": state.declared_name},
    )
    assert isinstance(failure, RequestACPFailure)
    assert failure.input_state is state
    assert (
        failure.input_disposition
        == state.public_status.replace("_", " ").capitalize() + " — input not retried"
    )
    assert decode_updates(encode_updates(RequestFailedUpdate(failure)))[0].failure == failure


def test_public_not_sent_and_ambiguous_unknown_do_not_invent_binding():
    from agent_comms.input_attempt import NotSentInput

    failure = ACPFailure.from_error(-32603, "Preflight failed", {"inputStatus": "not_sent"})
    assert failure.input_state is NotSentInput
    assert failure.input_disposition == "Not sent — input not retried"
    unknown = ACPFailure.from_error(-32603, "Outcome uncertain", {"inputStatus": "unknown"})
    assert unknown.input_state is None
    assert unknown.input_disposition == "Unconfirmed — input not retried"


def test_new_display_case_needs_only_its_declaration():
    class FixtureDisplayFailure(ACPFailure):
        classification_priority = 200
        title = "Fixture feedback"

        @classmethod
        def matches(cls, code, detail, diagnostics=()):
            return detail == "T2 display extension fixture"

    failure = ACPFailure.from_error(-32603, "T2 display extension fixture")
    assert isinstance(failure, FixtureDisplayFailure)
    assert failure.input_disposition == "Unconfirmed — input not retried"
    assert decode_updates(encode_updates(RequestFailedUpdate(failure)))[0].failure == failure


@pytest.mark.parametrize(
    "payload",
    [
        "The provider usage limit has been reached",
        [None, 400, {"error": {"details": "The provider usage limit has been reached"}}],
        {"error": {"data": [False, {"reason": "The provider usage limit has been reached"}]}},
        {"reason": json.dumps([{"details": "The provider usage limit has been reached"}])},
    ],
)
def test_provider_shapes_preserve_reason_without_granting_input_authority(payload):
    failure = ACPFailure.from_error(-32603, "Internal error", payload)
    assert isinstance(failure, ProviderQuotaFailure)
    assert failure.detail == "The provider usage limit has been reached"
    assert failure.input_disposition == "Unconfirmed — input not retried"


def test_only_original_root_metadata_owns_disposition_and_published_receipt():
    from agent_comms.input_attempt import NotSentInput

    nested = {"error": {"reason": "Provider refused", "inputStatus": "not_sent"}}
    assert ACPFailure.from_error(-32603, "Internal error", nested).input_state is None
    original = PromptFailureReceipt(
        RequestACPFailure(-32603, "Original preflight refusal", NotSentInput), True
    )
    data = {**original.error_data(), "error": {"reason": "Unrelated nested provider detail"}}
    assert PromptFailureReceipt.from_error(-32603, "Internal error", data) == original


@pytest.mark.parametrize("payload", [None, False, 37, [], {}, {"details": "Internal error"}])
def test_empty_or_nontext_provider_payload_retains_rpc_message(payload):
    failure = ACPFailure.from_error(-32603, "Original RPC refusal", payload)
    assert failure.detail == "Original RPC refusal"
    assert failure.input_disposition == "Unconfirmed — input not retried"


def test_encoded_provider_text_cannot_supply_original_root_disposition():
    data = json.dumps({"reason": "Provider refused", "inputStatus": "not_sent"})
    failure = ACPFailure.from_error(-32603, "Internal error", data)
    assert "Provider refused" in failure.detail
    assert failure.input_state is None


def test_canonical_receipt_detail_is_not_reinterpreted_as_provider_json():
    from agent_comms.input_attempt import StartedInput

    detail = '{"message" : "Internal error", "details" : [ "verbatim receipt" ]}'
    receipt = PromptFailureReceipt(RequestACPFailure(-32603, detail, StartedInput), True)
    assert PromptFailureReceipt.from_error(-32603, "Internal error", receipt.error_data()) == receipt


def test_provider_array_order_and_unknown_json_text_survive():
    payload = [{"details": "First provider reason"}, {"details": "Second provider reason"}]
    assert ACPFailure.from_error(-32603, "Internal error", payload).detail == "First provider reason"
    unknown = '{"vendorDiagnostic" : {"future" : 17}}'
    assert ACPFailure.from_error(-32603, "RPC refused", unknown).detail == unknown
