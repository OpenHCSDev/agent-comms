"""Both directions of the owner socket are declared families, decoded once."""

import pytest

from agent_comms.field_codec import MalformedRecord
from agent_comms.owner_protocol import (
    ClientLine,
    ClientPermissionResponse,
    OwnerAnnouncement,
    OwnerFailure,
    OwnerLine,
    OwnerPermissionRequest,
    OwnerReady,
    OwnerResult,
    OwnerRpcError,
    OwnerUpdate,
)


def test_every_declared_line_round_trips_through_its_family():
    owner_lines = (
        OwnerAnnouncement(),
        OwnerReady(ready={"agentComms": {"updates": []}}, controller_token="a" * 64),
        OwnerUpdate(update={"sessionUpdate": "agent_message_chunk"}, meta={"agentComms": {}}),
        OwnerPermissionRequest(id="p", tool_call={"toolCallId": "call"},
                               options=[{"optionId": "allow"}]),
        OwnerResult(result=["any", {"json": 1}]),
        OwnerFailure(error="refused", rpc_error=OwnerRpcError(-32602, "Invalid params", {"r": 1})),
        OwnerFailure(error="plain"),
    )
    assert {type(line) for line in owner_lines} == set(OwnerLine.members_with(OwnerLine))
    for line in owner_lines:
        assert OwnerLine.read(line.wire()) == line
    client_lines = (ClientPermissionResponse.cancelled("p"),)
    assert {type(line) for line in client_lines} == set(ClientLine.members_with(ClientLine))
    for line in client_lines:
        assert ClientLine.read(line.wire()) == line


@pytest.mark.parametrize(
    ("family", "line", "refusal"),
    [
        (OwnerLine, b'{"ready": {}, "controllerToken": "' + b"a" * 64 + b'"}',
         "Expected a string family kind"),
        (OwnerLine, b'{"kind": "owner_retired"}', "Invalid OwnerLine name"),
        (OwnerLine, b'{"kind": "owner_result"}', "Missing required fields for OwnerResult"),
        (ClientLine, b'{"permissionResponse": {"id": "p", "outcome": "cancelled"}}',
         "Expected a string family kind"),
        (ClientLine, b'{"kind": "owner_result", "result": 1}', "Invalid ClientLine name"),
        (ClientLine, b'{"kind": "client_permission_response", "id": "p", "outcome": {}}',
         "Invalid ClientPermissionResponse"),
    ],
    ids=["owner-untagged", "owner-unknown", "owner-malformed",
         "client-untagged", "client-unknown", "client-malformed"],
)
def test_undeclared_line_refuses_in_both_directions(family, line, refusal):
    with pytest.raises(MalformedRecord, match=refusal):
        family.read(line)
