from agent_comms.acp_failure import ACPFailure

"""Declared extension decoding is strict and shares its owner with consumers."""
import pytest

from agent_comms.acp_extension import (
    AgentCommsUpdate,
    AvailableQueueProjection,
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
    RequestFailedUpdate,
    PromptCancelledUpdate,
    SelectedWriteAcceptedUpdate,
    TextRouteUpdate,
    TranscriptChangedUpdate,
    TranscriptSnapshotUpdate,
    TurnChangedUpdate,
    UnavailableCursorObservation,
    decode_updates,
    encode_updates,
)
from agent_comms.acp_failure import BackendDeliveryFailure
from agent_comms.agent_events import CompactionStart
from agent_comms.compaction_states import CompactionPublishedMetadata
from agent_comms.pi_payloads import McpLiveReceipt
from agent_comms.thread_identity import OwnerIdentity, ThreadIncarnation
from agent_comms.transcripts import TranscriptCursor, TranscriptPage
from agent_comms.turn_lease import ActiveTurn, TurnState


def test_declared_family_roundtrip_and_strict_boundary(tmp_path):
    from agent_comms.comms import wire
    from agent_comms.threads import Thread
    comms = wire(tmp_path)
    comms.registry.declare(Thread("pilot", frozenset(), str(tmp_path)))
    read = comms.transcripts.capture_page_read("pilot")
    owner = OwnerIdentity(ThreadIncarnation("pilot", 1.0), 1)
    queue_scope = QueueScope("pilot", owner, 123)
    samples = (
        RequestFailedUpdate(ACPFailure.from_error(-32603, "The usage limit has been reached")),
        PromptCancelledUpdate(None),
        SelectedWriteAcceptedUpdate("operation", 1, "claim"),
        CursorAdvancedUpdate(
            CursorEnvelope(
                CursorScope("pilot", "a" * 32, owner, 123), 1, UnavailableCursorObservation()
            )
        ),
        QueueChangedUpdate(queue_scope, 1, AvailableQueueProjection((QueueItem("input", "text"),))),
        InputStartedUpdate("input", "text", queue_scope, 2, "b" * 32),
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
            read.read(), read.identity
        ),
        InputDeliveryChangedUpdate("input"),
        CompactionPublishedUpdate(
            CompactionPublishedMetadata(commit_id="commit", entry_id="entry", revision="revision", leaf_id="leaf")
        ),
        McpClientReceiptUpdate(
            "turn", McpLiveReceipt(1, "pi-mcp-client", "a" * 32, "running", "turn", ())
        ),
        TurnChangedUpdate(TurnState(ActiveTurn("turn", 123, started_at=1.0))),
        TurnChangedUpdate(TurnState(finished_turn_id="turn")),
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


def test_request_family_strict_boundary():
    from agent_comms.acp_extension import (
        ClearQueueRequest,
        CommsRequest,
        CompactRequest,
        QueuePromptRequest,
        SelectedWriteRequest,
        SendNowRequest,
        SteerPromptRequest,
        decode_request,
        encode_request,
    )

    samples = (
        QueuePromptRequest("queue", True),
        SteerPromptRequest("steer", False),
        CompactRequest("focus"),
        ClearQueueRequest(),
        SendNowRequest(),
        SelectedWriteRequest(1, "message", "/file", "bytes"),
    )
    assert {type(value) for value in samples} == set(CommsRequest.members_with(CommsRequest))
    for value in samples:
        assert decode_request(encode_request(value)) == value
    for value in (
        {"agentComms": {"clearQueue": True}},
        {"agentComms": {"request": {"kind": "compact", "unknown": True}}},
    ):
        with pytest.raises(ValueError):
            decode_request(value)
