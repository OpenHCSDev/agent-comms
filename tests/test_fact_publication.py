"""Fact publication keeps transport envelopes, validation and acknowledgements exact."""

import subprocess
import sys

import pytest

from agent_comms.acp_extension import decode_updates
from agent_comms.execution.acp_failure import (
    BackendDeliveryFailure,
    FailedNativeTurnOutcome,
    InputFailedUpdate,
    RequestACPFailure,
    RequestFailedUpdate,
    RequestFailure,
)
from agent_comms.execution.controller import NO_CONTROLLER
from agent_comms.execution.session_updates import encode_updates
from agent_comms.nominal.field_codec import AcpSessionId
from agent_comms.text_route_update import TextRouteUpdate
from agent_comms.transport.acp_update_sink import AcpUpdateSink


class Destination(AcpUpdateSink):
    def __init__(self, accepted):
        self.accepted = accepted
        self.updates = []

    async def publish_update(self, *, session_id, update):
        self.updates.append((session_id, update))
        return self.accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("text", [None, "", "original input"])
async def test_failed_turn_keeps_fact_order_and_destination_result(accepted, text):
    destination = Destination(accepted)
    failure = RequestACPFailure(-32603, "original failure")
    original = FailedNativeTurnOutcome(RequestFailure(failure, False), failed_input_text=text)
    result = await original.publish(destination, "session")
    assert result.request.notification_published is accepted
    assert not original.request.notification_published
    [(session, update)] = destination.updates
    assert session == "session"
    expected = (TextRouteUpdate(), RequestFailedUpdate(failure, original.source))
    if text:
        expected += (InputFailedUpdate(text, failure),)
    assert decode_updates(update.field_meta) == expected
    assert update.model_dump(mode="json", by_alias=True, exclude_none=True) == {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": ""},
        "_meta": encode_updates(*expected),
    }


@pytest.mark.asyncio
async def test_already_encoded_metadata_is_published_without_reencoding():
    destination = Destination(True)
    metadata = {"futureExtension": {"value": [1, "two", None]}}
    assert await destination.publish_metadata(
        session_id=AcpSessionId("session"), metadata=metadata,
    )
    [(_, update)] = destination.updates
    assert update.field_meta == metadata


@pytest.mark.asyncio
async def test_no_controller_still_validates_before_declining_publication():
    assert not await NO_CONTROLLER.publish_facts(
        TextRouteUpdate(), session_id=AcpSessionId("session"),
    )
    invalid = InputFailedUpdate(7, BackendDeliveryFailure("failure"))
    with pytest.raises(TypeError, match="str"):
        await NO_CONTROLLER.publish_facts(invalid, session_id=AcpSessionId("session"))


def test_headless_fact_encoding_and_publication_do_not_load_acp():
    result = subprocess.run([sys.executable, "-c", """
import asyncio
import sys
from agent_comms.execution.controller import NO_CONTROLLER
from agent_comms.nominal.field_codec import AcpSessionId
from agent_comms.text_route_update import TextRouteUpdate; from agent_comms.execution.session_updates import encode_updates
assert encode_updates(TextRouteUpdate())['agentComms']
assert asyncio.run(NO_CONTROLLER.publish_facts(
    TextRouteUpdate(), session_id=AcpSessionId('headless'))) is False
assert 'acp.schema' not in sys.modules
"""], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
