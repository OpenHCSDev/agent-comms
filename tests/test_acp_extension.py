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
from agent_comms.acp_extension import (
    CursorAdvancedUpdate, CursorEnvelope, CursorScope, UnavailableCursorObservation,
    QueueChangedUpdate, QueueScope, QueueItem, AvailableQueueProjection, InputStartedUpdate,
    CoordinationChangedUpdate, GoalChangedUpdate,
)
from agent_comms.thread_identity import OwnerIdentity, ThreadIncarnation


def test_declared_family_roundtrip_and_strict_boundary():
    owner = OwnerIdentity(ThreadIncarnation('pilot', 1.0), 1)
    queue_scope = QueueScope('pilot', owner, 123)
    samples = (
        CursorAdvancedUpdate(CursorEnvelope(CursorScope('pilot', 'a'*32, owner, 123), 1, UnavailableCursorObservation())),
        QueueChangedUpdate(queue_scope, 1, AvailableQueueProjection((QueueItem('input', 'text'),))),
        InputStartedUpdate('input', 'text', queue_scope, 2),
        CoordinationChangedUpdate(owner.incarnation, '/home/ts/wt/pilot', 123, '/home/ts/wt/pilot', None, None, 'Pilot', None),
        GoalChangedUpdate(None, None),
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
