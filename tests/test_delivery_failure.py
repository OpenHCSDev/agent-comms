import pytest

from agent_comms.acp_extension import (
    BackendDeliveryFailure,
    ClaimBlockedFailure,
    InputFailedUpdate,
    decode_updates,
    encode_updates,
)
from agent_comms.delivery_failure import delivery_failure
from agent_comms.errors import (
    HumanClaimBlockedError,
    HumanSendError,
    HumanSendRefusedError,
    ToolRefusedError,
)


@pytest.mark.parametrize(
    "error, member",
    (
        (OSError("refused"), BackendDeliveryFailure),
        (HumanSendRefusedError(ValueError("refused")), BackendDeliveryFailure),
        (HumanClaimBlockedError("reserved gap"), ClaimBlockedFailure),
    ),
)
def test_delivery_failure_decodes_once(error, member):
    value = delivery_failure(error)
    assert type(value) is member
    assert value.description == str(error)
    update = InputFailedUpdate("body", value)
    assert decode_updates(encode_updates(update)) == (update,)


@pytest.mark.parametrize(
    "error",
    (HumanSendRefusedError(ValueError("rolled back")), HumanClaimBlockedError("reserved gap")),
)
def test_human_send_failures_reach_the_ui_catch(error):
    with pytest.raises(HumanSendError):
        raise error


def test_rolled_back_human_send_remains_a_tool_refusal():
    with pytest.raises(ToolRefusedError):
        raise HumanSendRefusedError(ValueError("rolled back"))
