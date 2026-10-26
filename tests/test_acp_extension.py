"""Declared extension decoding is strict and shares its owner with consumers."""

from agent_comms.repo import Checkout
import pytest

from agent_comms.acp_extension import (
    AgentCommsUpdate,
    AvailableQueueProjection,
    CompactionChangedUpdate,
    CompactionCommittedUpdate,
    ContextAnnotatedUpdate,
    CoordinationChangedUpdate,
    CursorAdvancedUpdate,
    CursorEnvelope,
    GoalChangedUpdate,
    InputDeliveryChangedUpdate,
    InputFailedUpdate,
    IdentifiedInputStarted,
    UnidentifiedInputStarted,
    LiveCursorScope,
    LiveQueueScope,
    McpClientAcknowledgementUpdate,
    NoQueueScope,
    PromptCancelledUpdate,
    QueueChangedUpdate,
    QueueItem,
    RequestFailedUpdate,
    SelectedWriteAcceptedUpdate,
    TextRouteUpdate,
    TranscriptChangedUpdate,
    TranscriptDraftUpdate,
    TranscriptSnapshotUpdate,
    TurnChangedUpdate,
    UnavailableCursorObservation,
    decode_updates,
    encode_updates,
)
from agent_comms.acp_failure import ACPFailure, BackendDeliveryFailure
from agent_comms.agent_events import CompactionStart
from agent_comms.child_process import ProcessIdentity
from agent_comms.context_reading import UnmeasuredContext
from agent_comms.field_codec import ClaimId, InputId, MessageId, RootId, TurnId
from agent_comms.goal_presentation import IdleGoalExecution
from agent_comms.goals import NoGoal
from agent_comms.pi_payloads import McpLiveAcknowledgement
from agent_comms.thread_identity import ClaimedIdentity, RecordedThreadIncarnation
from agent_comms.threads import Thread
from agent_comms.transcripts import NativeTranscriptCursor, TranscriptPage
from agent_comms.turn_context import ContextManifest, NextContextTurn
from agent_comms.turn_lease import ActiveTurn, FinishedTurn, TurnState


def test_declared_family_roundtrip_and_strict_boundary(tmp_path):
    from conftest import installed_comms
    comms = installed_comms(tmp_path)
    comms.registry.declare(Thread('pilot', frozenset(), Checkout(tmp_path)))
    read = comms.transcripts.capture_page_read("pilot")
    owner = ClaimedIdentity(RecordedThreadIncarnation("pilot", 1.0), 1)
    queue_scope = LiveQueueScope(
        session_id="pilot", claimed_identity=owner, owner=ProcessIdentity(123, 1),
    )
    samples = (
        ContextAnnotatedUpdate(
            ContextManifest(owner.incarnation, NextContextTurn(), (), "source-codec-control"),
            (),
        ),
        RequestFailedUpdate(ACPFailure.from_detail(-32603, "The usage limit has been reached")),
        PromptCancelledUpdate(None),
        SelectedWriteAcceptedUpdate(ClaimId("c" * 32), 1, "claim"),
        CursorAdvancedUpdate(
            CursorEnvelope(
                LiveCursorScope(
                    session_id="pilot",
                    wire_root_id=RootId("a" * 32),
                    claimed_identity=owner,
                    owner=ProcessIdentity(123, 1),
                ),
                1,
                UnavailableCursorObservation(),
            )
        ),
        QueueChangedUpdate(
            queue_scope, 1, AvailableQueueProjection((QueueItem(InputId.mint(), "text"),)),
        ),
        IdentifiedInputStarted("text", queue_scope, 2, InputId("b" * 32), InputId("a" * 32)),
        UnidentifiedInputStarted("text", queue_scope, 2, InputId("b" * 32)),
        CoordinationChangedUpdate(
            owner.incarnation,
            "/home/ts/wt/pilot",
            ProcessIdentity(123, 1),
            "/home/ts/wt/pilot",
            "openai-codex/gpt-6.1-sol",
            None,
            "Pilot",
            UnmeasuredContext(),
        ),
        GoalChangedUpdate(NoGoal(), IdleGoalExecution()),
        CompactionChangedUpdate(CompactionStart()),
        CompactionCommittedUpdate("summary", "first-kept"),
        TranscriptSnapshotUpdate(read.read(), read.identity),
        TranscriptDraftUpdate(read.identity, None),
        InputDeliveryChangedUpdate(),
        McpClientAcknowledgementUpdate(
            TurnId.mint(), McpLiveAcknowledgement(
                1, "pi-mcp-client", InputId("a" * 32), "running", "turn", ()
            )
        ),
        TurnChangedUpdate(
            TurnState(
                ActiveTurn(
                    TurnId('9b473845f684444f80a26586ae298d2b'),
                    ProcessIdentity(123, 1),
                    started_at=1.0,
                )
            )
        ),
        TurnChangedUpdate(TurnState(finished=FinishedTurn(TurnId('9b473845f684444f80a26586ae298d2b')))),
        TextRouteUpdate(),
        TranscriptChangedUpdate(NativeTranscriptCursor("session.jsonl", 42)),
        InputFailedUpdate("prompt", BackendDeliveryFailure("provider refused")),
    )
    assert {type(item) for item in samples} == set(AgentCommsUpdate.members_with(AgentCommsUpdate))
    for sample in samples:
        try:
            assert decode_updates(encode_updates(sample)) == (sample,)
        except (TypeError, ValueError) as error:
            error.add_note(f"ACP sample {type(sample).__name__}: {encode_updates(sample)!r}")
            raise
    assert decode_updates(encode_updates(*samples)) == samples
    for invalid in (
        {"agentComms": {"turnStarted": True}},
        {"agentComms": {"updates": [{"kind": "unregistered"}]}},
        {"agentComms": {"updates": [], "extra": True}},
    ):
        with pytest.raises((TypeError, ValueError)):
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
        SelectedWriteRequest(1, MessageId.mint("message"), "/file", "bytes"),
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


def test_only_an_unidentified_start_without_a_live_queue_posts_itself():
    owner = ClaimedIdentity(RecordedThreadIncarnation("pilot", 1.0), 1)
    live = LiveQueueScope(session_id="pilot", claimed_identity=owner, owner=ProcessIdentity(123, 1))
    native, input_id = InputId.mint(), InputId.mint()
    posted = []
    for update in (
        UnidentifiedInputStarted("routed", NoQueueScope(), None, native),
        UnidentifiedInputStarted("routed", live, 1, native),
        IdentifiedInputStarted("queued", NoQueueScope(), None, native, input_id),
        IdentifiedInputStarted("queued", live, 1, native, input_id),
    ):
        update.when_unidentified(lambda update=update: posted.append(update))
    assert [(type(update), type(update.scope)) for update in posted] == [
        (UnidentifiedInputStarted, NoQueueScope)]
