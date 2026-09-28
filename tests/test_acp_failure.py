"""The shared failure owner preserves external reasons and declared input states."""

import json

import pytest

from agent_comms.acp_extension import RequestFailedUpdate, decode_updates, encode_updates
from agent_comms.acp_failure import ACPFailure, ProviderQuotaFailure, RequestACPFailure
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
    assert failure.input_disposition == "UNKNOWN — input not retried"
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
        == state.declared_name.replace("_", " ").capitalize() + " — input not retried"
    )
    assert decode_updates(encode_updates(RequestFailedUpdate(failure)))[0].failure == failure
