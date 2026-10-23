"""Both directions of the owner socket are declared families, decoded once."""


from agent_comms.runtime import ClientPermissionResponse

from agent_comms.owner_protocol import (
    ClientLine,
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


