"""Partial external snapshots cannot claim a complete native session identity."""

import json

import pytest

from agent_comms.native_attestation import ObservedAttestation
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.pi_payloads import StateData
from agent_comms.pi_rpc import PiRpcChannel


def snapshot(command, data):
    return PiRpcChannel.decode_record(
        json.dumps({"type": "response", "command": command, "success": True, "data": data}).encode()
        + b"\n",
        strict=True,
    ).data


@pytest.mark.parametrize("command", ["get_state", "get_session_stats"])
@pytest.mark.parametrize(
    "partial,conflict",
    [
        ({}, False),
        ({"sessionId": None, "sessionFile": None}, False),
        ({"sessionId": "", "sessionFile": ""}, False),
        ({"sessionId": "selected"}, False),
        ({"sessionFile": "/selected.jsonl"}, False),
        ({"sessionId": "foreign"}, True),
        ({"sessionFile": "/foreign.jsonl"}, True),
        ({"sessionId": "foreign", "sessionFile": ""}, True),
        ({"sessionId": None, "sessionFile": "/foreign.jsonl"}, True),
    ],
)
def test_partial_native_snapshot_only_conflicts_on_known_components(command, partial, conflict):
    selected = StateData(session_id="selected", session_file="/selected.jsonl")
    observed = snapshot(command, partial)
    assert observed.identity is None
    assert observed.conflicts(selected) is conflict
    assert selected.conflicts(observed) is conflict
    assert ObservedAttestation(selected).conflicts(observed) is conflict
    # Complementary observations do not silently synthesize a complete identity.
    assert not snapshot(command, {"sessionId": "foreign"}).conflicts(
        snapshot(command, {"sessionFile": "/selected.jsonl"})
    )


@pytest.mark.parametrize("command", ["get_state", "get_session_stats"])
def test_complete_native_snapshot_uses_existing_identity_owner(command):
    observed = snapshot(command, {"sessionId": "selected", "sessionFile": "/selected.jsonl"})
    assert observed.identity == NativeSessionIdentity("selected", "/selected.jsonl")
    assert not observed.conflicts(observed)
    assert type(observed).from_wire(observed.to_wire()) == observed
