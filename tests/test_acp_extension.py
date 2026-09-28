"""Declared extension decoding is strict and shares its owner with consumers."""

import pytest

from agent_comms.acp_extension import (
    AgentCommsUpdate,
    BackendDeliveryFailure,
    InputFailedUpdate,
    TextRouteUpdate,
    TranscriptChangedUpdate,
    TurnSettledUpdate,
    TurnStartedUpdate,
    decode_updates,
    encode_updates,
)
from agent_comms.transcripts import TranscriptCursor


def test_declared_family_roundtrip_and_strict_boundary():
    samples = (
        TurnStartedUpdate("turn", 1.0, "thinking", None),
        TurnSettledUpdate("turn"),
        TextRouteUpdate(None),
        TranscriptChangedUpdate(TranscriptCursor("session.jsonl", 42)),
        InputFailedUpdate("prompt", BackendDeliveryFailure("provider refused")),
    )
    assert {type(item) for item in samples} == set(AgentCommsUpdate.members_with(AgentCommsUpdate))
    assert decode_updates(encode_updates(*samples)) == samples
    for invalid in (
        {"agentComms": {"turnStarted": True}},
        {"agentComms": {"updates": [{"kind": "unregistered"}]}},
        {"agentComms": {"updates": [], "extra": True}},
    ):
        with pytest.raises(ValueError):
            decode_updates(invalid)
