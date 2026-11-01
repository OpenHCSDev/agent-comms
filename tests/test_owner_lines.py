"""Both directions of the owner socket are declared families, decoded once."""


import pytest

from agent_comms.coordination_errors import Refused
from agent_comms.errors import GoalAttemptError, StaleAttemptError, UnresolvedAttemptError
from agent_comms.goal_attempts import MissingGoalGenerationError, ReservationConflictError, StorageUncertainError
from agent_comms.nominal.field_codec import GoalId
from agent_comms.runtime_server import ClientPermissionResponse

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


@pytest.mark.parametrize("refusal", (
    UnresolvedAttemptError("The attempt is unresolved."),
    StaleAttemptError("The attempt changed."),
    ReservationConflictError("The generation is reserved."),
    MissingGoalGenerationError(GoalId.mint()),
))
def test_expected_attempt_refusals_keep_their_rpc_contract(refusal):
    assert isinstance(refusal, GoalAttemptError)
    decoded = OwnerLine.read(OwnerFailure.refused(refusal).wire())
    with pytest.raises(Refused) as raised:
        decoded.reply()
    assert raised.value.code == refusal.code
    assert raised.value.message == refusal.message


def test_storage_uncertainty_is_not_an_invalid_goal_request():
    failure = StorageUncertainError("Write outcome is uncertain.")
    assert isinstance(failure, GoalAttemptError)
    assert not isinstance(failure, Refused)
    decoded = OwnerLine.read(OwnerFailure.of(failure).wire())
    with pytest.raises(RuntimeError, match="Write outcome is uncertain"):
        decoded.reply()


