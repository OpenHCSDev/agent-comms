"""Declared extension decoding is strict and shares its owner with consumers."""

import pytest

from agent_comms.acp_extension import (
    AgentCommsUpdate,
    AvailableQueueProjection,
    BackendDeliveryFailure,
    CompactionChangedUpdate,
    CompactionCommittedUpdate,
    CompactionPublishedUpdate,
    CoordinationChangedUpdate,
    CursorAdvancedUpdate,
    CursorEnvelope,
    CursorScope,
    GoalChangedUpdate,
    InputDeliveryChangedUpdate,
    InputFailedUpdate,
    InputStartedUpdate,
    McpClientReceiptUpdate,
    QueueChangedUpdate,
    QueueItem,
    QueueScope,
    TextRouteUpdate,
    TranscriptChangedUpdate,
    TranscriptSnapshotUpdate,
    TurnSettledUpdate,
    TurnStartedUpdate,
    UnavailableCursorObservation,
    decode_updates,
    encode_updates,
)
from agent_comms.agent_events import CompactionStart
from agent_comms.compaction_states import CompactionPublishedMetadata
from agent_comms.pi_payloads import McpLiveReceipt
from agent_comms.thread_identity import OwnerIdentity, ThreadIncarnation
from agent_comms.transcripts import TranscriptCursor, TranscriptPage


def test_declared_family_roundtrip_and_strict_boundary():
    owner = OwnerIdentity(ThreadIncarnation("pilot", 1.0), 1)
    queue_scope = QueueScope("pilot", owner, 123)
    samples = (
        CursorAdvancedUpdate(
            CursorEnvelope(
                CursorScope("pilot", "a" * 32, owner, 123), 1, UnavailableCursorObservation()
            )
        ),
        QueueChangedUpdate(queue_scope, 1, AvailableQueueProjection((QueueItem("input", "text"),))),
        InputStartedUpdate("input", "text", queue_scope, 2),
        CoordinationChangedUpdate(
            owner.incarnation,
            "/home/ts/wt/pilot",
            123,
            "/home/ts/wt/pilot",
            None,
            None,
            "Pilot",
            None,
        ),
        GoalChangedUpdate(None, None),
        CompactionChangedUpdate(CompactionStart()),
        CompactionCommittedUpdate("commit", "summary"),
        TranscriptSnapshotUpdate(
            TranscriptPage((), TranscriptCursor("s", 0), TranscriptCursor("s", 1), False, False)
        ),
        InputDeliveryChangedUpdate("input"),
        CompactionPublishedUpdate(
            CompactionPublishedMetadata("commit", "entry", "revision", "leaf")
        ),
        McpClientReceiptUpdate(
            "turn", McpLiveReceipt(1, "pi-mcp-client", "a" * 32, "running", "turn", ())
        ),
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
