import pytest

from agent_comms.acp_extension import (
    BackendDeliveryFailure,
    ClaimBlockedFailure,
    InputFailedUpdate,
    decode_updates,
    encode_updates,
)
from agent_comms.errors import (
    HumanClaimBlockedError,
    HumanSendRefusedError,
)
from agent_comms.messaging.delivery_failure import delivery_failure


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


class RefusingView:
    """The one presentation a conversation view keeps (Toad 62e938ce deleted delivery_blocked)."""

    def __init__(self) -> None:
        self.refused: list[tuple[str, str]] = []

    def delivery_refused(self, description: str, body: str) -> None:
        self.refused.append((description, body))


@pytest.mark.parametrize("error", (
    OSError("refused"), HumanSendRefusedError(ValueError("refused")),
    HumanClaimBlockedError("reserved gap"),
))
def test_every_delivery_failure_presents_as_the_refusal_a_view_keeps(error):
    view = RefusingView()
    delivery_failure(error).present(view, "body")
    assert view.refused == [(str(error), "body")]
