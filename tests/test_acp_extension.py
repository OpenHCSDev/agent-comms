"""Declared extension decoding is strict and shares its owner with consumers."""

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
    IdentifiedInputStarted,
    InputDeliveryChangedUpdate,
    InputFailedUpdate,
    LiveQueueScope,
    McpClientAcknowledgementUpdate,
    NoQueueScope,
    PromptCancelledUpdate,
    QueueChangedUpdate,
    QueueItem,
    RequestFailedUpdate,
    SelectedWriteAcceptedUpdate,
    TextNoticeUpdate,
    TextRouteUpdate,
    TranscriptChangedUpdate,
    TranscriptDraftUpdate,
    TranscriptSnapshotUpdate,
    TurnChangedUpdate,
    UnavailableCursorObservation,
    UnidentifiedInputStarted,
    decode_updates,
    encode_updates,
)
from agent_comms.agent_events import CompactionStart
from agent_comms.execution.acp_failure import ACPFailure, BackendDeliveryFailure
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.context_reading import UnmeasuredContext
from agent_comms.execution.pi_wire import McpLiveAcknowledgement
from agent_comms.execution.transcript_sources import NativeTranscriptCursor
from agent_comms.execution.turn_context import (
    ContextManifest,
    NextContextTurn,
)
from agent_comms.execution.turn_lease import (
    ActiveTurn,
    FinishedTurn,
    TurnState,
)
from agent_comms.nominal.field_codec import (
    AcpSessionId,
    AssignmentId,
    ClaimId,
    InputId,
    MessageId,
    RootId,
    TurnId,
)
from agent_comms.planning.goal_presentation import IdleGoalExecution
from agent_comms.planning.goals import NoGoal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import (
    ClaimedIdentity,
    RecordedThreadIncarnation,
)
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.private_path import MissingFile
from agent_comms.transcript.transcript_drafts import NoDraft
from agent_comms.transport.attachment_scope import LiveCursorScope


def test_declared_family_roundtrip_and_strict_boundary(tmp_path):
    from conftest import installed_comms
    comms = installed_comms(tmp_path)
    comms.registry.declare(NativeThread('pilot', frozenset(), Checkout(tmp_path)))
    read = comms.transcripts.capture_page_read("pilot")
    owner = ClaimedIdentity(RecordedThreadIncarnation("pilot", 1.0), 1)
    queue_scope = LiveQueueScope(
        session_id=AcpSessionId("pilot"), claimed_identity=owner, owner=ProcessIdentity(123, 1),
    )
    from agent_comms.acp_extension import NativeOutputCommittedUpdate, NativeRoundCommittedUpdate
    from agent_comms.execution.native_output import AssistantOutput, NativeRecordLocation
    from agent_comms.execution.native_session_reopen import PiSessionId
    from agent_comms.execution.native_record_file import PresentSessionCut
    from agent_comms.execution.turn_item import NativeItemId
    from agent_comms.messaging.sent_message import AssignedSourceIdentity
    from agent_comms.storage.private_path import FileRevision
    from agent_comms.transcript.transcripts import NativeCommittedPrefix
    sample_path = tmp_path / "session.jsonl"
    sample_path.write_text("{}\n")
    committed = NativeCommittedPrefix(
        NativeRecordLocation(PiSessionId("session"), str(tmp_path / "session.jsonl"),
                             NativeItemId("entry"), 1, 3),
        PresentSessionCut(FileRevision.from_stat(sample_path.stat()), 3),
        AssignedSourceIdentity(read.identity.root, read.identity.thread.incarnation),
    )
    samples = (
        ContextAnnotatedUpdate(
            ContextManifest(thread=owner.incarnation, turn=NextContextTurn(), segments=(), counter="source-codec-control"),
            (),
        ),
        RequestFailedUpdate(ACPFailure.from_detail(-32603, "The usage limit has been reached")),
        PromptCancelledUpdate(None),
        TextNoticeUpdate(),
        SelectedWriteAcceptedUpdate(ClaimId("c" * 32), 1, AssignmentId("d" * 32)),
        CursorAdvancedUpdate(
            CursorEnvelope(
                LiveCursorScope(
                    session_id=AcpSessionId("pilot"),
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
            Checkout(tmp_path),
            "openai-codex/gpt-6.1-sol",
            None,
            "Pilot",
            UnmeasuredContext(),
        ),
        GoalChangedUpdate(NoGoal(), IdleGoalExecution()),
        CompactionChangedUpdate(CompactionStart()),
        CompactionCommittedUpdate("summary"),
        TranscriptSnapshotUpdate(read.read(), read.identity),
        TranscriptDraftUpdate(read.identity, NoDraft()),
        InputDeliveryChangedUpdate(),
        NativeOutputCommittedUpdate(committed, AssistantOutput()),
        NativeRoundCommittedUpdate(committed),
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
        TranscriptChangedUpdate(NativeTranscriptCursor("session.jsonl", 42, file=MissingFile())),
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
        SelectedWriteRequest(
            source_seq=1, source_message_id=MessageId.mint("message"),
            resource="/file", contents="bytes",
        ),
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
    live = LiveQueueScope(session_id=AcpSessionId("pilot"), claimed_identity=owner, owner=ProcessIdentity(123, 1))
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
