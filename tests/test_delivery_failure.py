import pytest

from agent_comms.acp_extension import (
    BackendDeliveryFailure, ClaimBlockedFailure, InputFailedUpdate, decode_updates, encode_updates,
)
from agent_comms.delivery_failure import delivery_failure
from agent_comms.errors import HumanClaimBlockedError, HumanSendRefusedError


@pytest.mark.parametrize("error, member", (
    (OSError("refused"), BackendDeliveryFailure),
    (HumanSendRefusedError("Message was not sent"), BackendDeliveryFailure),
    (HumanClaimBlockedError("reserved gap"), ClaimBlockedFailure),
))
def test_delivery_failure_decodes_once(error, member):
    value = delivery_failure(error)
    assert type(value) is member
    assert value.description == str(error)
    update = InputFailedUpdate("body", value)
    assert decode_updates(encode_updates(update)) == (update,)
