"""Authored archived deliveries use recorded owners, not live availability."""

from agent_comms.wire_log import WireLog

from conftest import publish_initial
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from agent_comms.agent_activity import RecordedRecipientActivity, UnavailableRecipientActivity
from agent_comms.assignment_states import (
    CompletedAssignment,
    DeliveredAssignment,
    EngagedAssignment,
)
from agent_comms.comms import Comms
from agent_comms.coordination_errors import DMScopeChanged, HistoricalSourceChanged
from agent_comms.coordination_schema import COORDINATION_SCHEMA_VERSION
from agent_comms.read_basis import HistoricalDisplay, ReadCursor
from agent_comms.read_ledger import ReadLedger
from agent_comms.thread_identity import AgentThreadRole, UserThreadRole
from agent_comms.threads import Thread
from agent_comms.wake_policy import FullWake

SENDER = "sender"  # the notification carries its sender's lookup; these tests read progress only


@pytest.fixture
def archived_notification(tmp_path, *, thread_registration):
    old, live = Comms(tmp_path / "old"), Comms(tmp_path / "live")
    for comms in (old, live):
        comms.registry.declare(thread_registration.new_thread('sender', frozenset(), str(tmp_path), created_at=10.0))
        comms.registry.declare(thread_registration.new_thread('reader', frozenset(), str(tmp_path), created_at=11.0, role=AgentThreadRole if comms is old else UserThreadRole))
        comms.registry.declare(thread_registration.new_thread('agent', frozenset(), str(tmp_path), created_at=12.0 if comms is old else 212.0))
    root_id = old.messaging.initialize_private_initial_protocol()
    messages = (
        publish_initial(old, "sender", "agent", "authored pending delivery"),
        publish_initial(old, "sender", "reader", "painted historical delivery"),
        publish_initial(old, "sender", "reader", "unpainted historical delivery"),
    )
    source = live.views.attach_history(old.root)
    return old, live, source, messages, root_id


def test_recorded_notifications_do_not_decode_registry_or_acquire_activity(archived_notification, monkeypatch):
    old, live, source, messages, _root_id = archived_notification
    preserved = {path: path.read_bytes() for path in (old.root / "registry.json", old.root / "bus.jsonl")}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Recorded notification borrowed live registry/activity")

    monkeypatch.setattr("agent_comms.registration.Registration.snapshot", forbidden)
    monkeypatch.setattr("agent_comms.agent_activity.AgentActivity.observe_recipients", forbidden)
    with live.bus.history.read_sources() as sources:
        results = source.notification_references(sources, tuple(message.reference for message in messages))
    notification, = results[messages[0].seq, messages[0].message_id]
    assert notification.recipient == "agent"
    assert notification.label == "Sent" and not notification.state.working
    assert "stopped" not in notification.state.detail
    assert notification.displayed_to == ()
    assert not (Path(source.root) / "coordination.sqlite3").exists()
    assert all(path.read_bytes() == original for path, original in preserved.items())
    live.bus.history.path.write_text("[]")
    with pytest.raises(HistoricalSourceChanged, match="detached"), live.bus.history.read_sources() as sources:
        source.notification_references(sources, (messages[0].reference,))


def test_recorded_display_is_sparse_and_exact_incarnation(archived_notification):
    _old, live, source, messages, _root_id = archived_notification
    document = live.bus.reads.read()
    with WireLog(Path(source.root) / 'bus.jsonl').opened_wire_snapshot() as opened:
        basis = opened.query_page_index(lambda index: live.bus.reads.capture_indexed(
            'reader', messages[1:], live.registry.snapshot(), opened, index,
            document=document, conversation_snapshot=source.provenance))
    HistoricalDisplay(source, basis.select((messages[1].seq,))).acknowledge(live.bus.history, live.registry)
    with live.bus.history.read_sources() as sources:
        results = source.notification_references(sources, tuple(message.reference for message in messages[1:]))
    painted, = results[messages[1].seq, messages[1].message_id]
    unpainted, = results[messages[2].seq, messages[2].message_id]
    assert painted.displayed_to == (source.provenance.require("reader").incarnation,)
    assert unpainted.displayed_to == ()
    assert live.bus.reads.cursor("reader", live.registry.snapshot()) == ReadCursor()
    ledger = ReadLedger.at(source.root)
    document = ledger.read()
    reused = replace(basis, viewer_created_at=111.0)
    with pytest.raises(DMScopeChanged, match="viewer changed"):
        HistoricalDisplay(source, reused).acknowledge(live.bus.history, live.registry)
    assert ledger.read() == document
    newer_namespace = replace(source.provenance, threads={
        **source.provenance.threads,
        "reader": replace(source.provenance.require("reader"), created_at=111.0),
    })
    assert ledger.historical_displayed_recipient(messages[1], painted.recipient_identity,
                                                newer_namespace, document=document) == ()


def test_response_evidence_rejects_mixed_sql_columns():
    from agent_comms.notification_assignment import AssignmentResponse

    for values in ((1, None, 2), (None, "message-id", 2), (1, "message-id", None)):
        with pytest.raises(ValueError, match="response evidence is incomplete"):
            AssignmentResponse.from_columns(*values)


def test_unshown_assignment_resolves_running_turn_as_timed_queue():
    from agent_comms.notification_assignment import (
        AssignmentActivityEvidence,
        NotShownContextEvidence,
        UnansweredResponse,
    )
    from agent_comms.notification_progress import QueuedProgress, SentProgress

    pending = AssignmentActivityEvidence(
        False, None, NotShownContextEvidence(), UnansweredResponse()
    ).resolve(pending_at_ms=1_000)
    queued = AssignmentActivityEvidence(
        False, "running-execution", NotShownContextEvidence(), UnansweredResponse()
    ).resolve(pending_at_ms=1_000)

    assert pending.progress == SentProgress(1_000)
    assert queued.progress == QueuedProgress(1_000)


def test_recorded_assignment_schema_refusal_is_read_only(archived_notification):
    _old, live, source, messages, _root_id = archived_notification
    database = Path(source.root) / "coordination.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version = {COORDINATION_SCHEMA_VERSION - 1}")
    database.chmod(0o600)
    original = database.read_bytes()
    with pytest.raises(ValueError, match="unsupported schema"), live.bus.history.read_sources() as sources:
        source.notification_references(sources, (messages[0].reference,))
    assert database.read_bytes() == original


def test_recorded_engagement_is_not_an_active_turn(archived_notification):
    old, _live, _source, messages, root_id = archived_notification
    recipient, = old.bus.log.read_delivery_cohort(root_id, messages[0].seq).audience.recipients
    assignment = EngagedAssignment.build(FullWake(), "authored-execution", "sender")
    from agent_comms.notification_assignment import (
        AssignmentActivity,
        UnansweredResponse,
    )
    from agent_comms.notification_progress import ShownToModelProgress

    activity = AssignmentActivity(
        triage_inflight=False,
        progress=ShownToModelProgress(1),
        response=UnansweredResponse(),
    )
    with pytest.raises(TypeError, match="activity"):
        assignment.notification(
            recipient, SENDER, observation=RecordedRecipientActivity(), updated_at_ms=1
        )
    recorded = assignment.notification(
        recipient,
        SENDER,
        observation=RecordedRecipientActivity(),
        activity=activity,
        updated_at_ms=1,
    )
    assert recorded.label.startswith("Read ") and not recorded.state.working
    assert recorded.state.detail == ""
    live = assignment.notification(
        recipient,
        SENDER,
        observation=UnavailableRecipientActivity(),
        activity=activity,
        updated_at_ms=1,
    )
    assert live.label.startswith("Read ") and not live.state.working
    assert live.state.detail == ""


def test_sent_recipient_progresses_through_durable_read_and_answer(archived_notification):
    old, _live, _source, messages, root_id = archived_notification
    recipient, = old.bus.log.read_delivery_cohort(
        root_id, messages[0].seq
    ).audience.recipients
    from agent_comms.message_reference import MessageReference
    from agent_comms.notification_assignment import (
        AnsweredResponse,
        AssignmentActivity,
        UnansweredResponse,
    )
    from agent_comms.notification_progress import QueuedProgress, SentProgress, ShownToModelProgress, SelectedReadProgress

    sent_activity = AssignmentActivity(
        triage_inflight=False,
        progress=SentProgress(1_000),
        response=UnansweredResponse(),
    )
    engagement = EngagedAssignment.build(FullWake(), "durable-execution", "sender")
    sent = engagement.notification(
        recipient,
        SENDER,
        observation=RecordedRecipientActivity(),
        activity=sent_activity,
        updated_at_ms=1_000,
    )
    queued_activity = replace(sent_activity, progress=QueuedProgress(1_000))
    queued = FullWake().initial_state().build_preengagement(FullWake()).notification(
        recipient,
        SENDER,
        observation=RecordedRecipientActivity(),
        activity=queued_activity,
        updated_at_ms=1_000,
    )
    read_activity = replace(sent_activity, progress=ShownToModelProgress(2_000))
    read = engagement.notification(
        recipient,
        SENDER,
        observation=RecordedRecipientActivity(),
        activity=read_activity,
        updated_at_ms=1_000,
    )
    answered_activity = replace(
        read_activity,
        response=AnsweredResponse(MessageReference(99, "durable-reply"), 3_000),
    )
    answered = CompletedAssignment.build(
        FullWake(), "durable-execution", "sender"
    ).notification(
        recipient,
        SENDER,
        observation=RecordedRecipientActivity(),
        activity=answered_activity,
        updated_at_ms=3_000,
    )

    delivered = DeliveredAssignment.build(FullWake(), None, None).notification(
        recipient,
        SENDER,
        observation=RecordedRecipientActivity(),
        activity=sent_activity,
        updated_at_ms=2_000,
    )
    delivered_answer = DeliveredAssignment.build(FullWake(), None, None).notification(
        recipient,
        SENDER,
        observation=RecordedRecipientActivity(),
        activity=replace(
            sent_activity,
            response=AnsweredResponse(MessageReference(99, "durable-reply"), 3_000),
        ),
        updated_at_ms=2_000,
    )

    assert sent.label.startswith("Sent ") and sent.state == SentProgress(1_000)
    assert queued.label.startswith("Queued ") and queued.state == QueuedProgress(1_000)
    assert read.label.startswith("Read ") and read.state == SelectedReadProgress(2_000)
    assert answered.label.startswith("Answered ") and answered.answered_at_ms == 3_000
    assert delivered.label.startswith("Read ") and delivered.state == ShownToModelProgress(2_000)
    assert delivered_answer.label.startswith("Answered ")
    assert delivered_answer.answer == MessageReference(99, "durable-reply")
    assert sent.recipient_identity == read.recipient_identity == answered.recipient_identity


def test_delivered_assignment_uses_its_recorded_conversation_response(archived_notification):
    from agent_comms.assignment_states import DeferredAssignment
    from agent_comms.coordination_cohort import accept_delivery_cohort
    from agent_comms.coordinator import Coordination

    old, _live, _source, messages, root_id = archived_notification
    original = messages[0]
    with Coordination(str(old.root / "coordination.sqlite3")) as store:
        assignment, = accept_delivery_cohort(
            old.bus, root_id, original.seq, store
        ).value.assignments
        paused = store.assignments.transition_preengagement(
            assignment.assignment_id,
            DeferredAssignment,
            expected_revision=assignment.revision,
        ).value
        store.assignments.transition_preengagement(
            assignment.assignment_id,
            DeliveredAssignment,
            expected_revision=paused.revision,
        )
    unrelated = old.messaging.send("agent", "sender", "unrelated later message").message
    notification, = old.views.message_notifications_for_references(
        (original.reference,)
    )[original.seq, original.message_id]
    assert unrelated.answers == ()
    assert notification.label.startswith("Read ")
    assert notification.answer is None

    import os
    from uuid import uuid4
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.input_disposition import InputDispositions
    from agent_comms.input_origin import WireInputOrigin

    owner = old.registry.require("agent")
    old.registry.register(
        replace(owner, process_identity=ProcessIdentity.capture(os.getpid()))
    )
    owner, generation = old.registry.live_owner_with_claim("agent")
    owner, _ = old.registry.lease_live_turn_with_claim(
        owner, "reply-turn", expected_generation=generation
    )
    lease = owner.require_turn_lease()
    inputs = InputDispositions(old.root)
    key = inputs.bus_key(original, owner)
    source_text = "delivered original"
    assert inputs.record(
        key,
        seq=original.seq,
        owner=owner.name,
        claim_scope=lease.claimed_generation,
        target=original.target,
        text=source_text,
        origin=WireInputOrigin(root_id, original.reference),
    )
    native_id = uuid4().hex
    assert inputs.bind(
        key,
        claim_scope=lease.claimed_generation,
        turn_id=lease.turn_id,
        native_id=native_id,
        text=source_text,
    )
    assert inputs.started(
        key, turn_id=lease.turn_id, native_id=native_id, text=source_text
    )
    response = old.messaging.send("agent", "sender", "recorded reply").message

    notification, = old.views.message_notifications_for_references(
        (original.reference,)
    )[original.seq, original.message_id]

    assert response.answers == (original.reference,)
    assert notification.label.startswith("Answered ")
    assert notification.answer == response.reference
    assert notification.answered_at_ms == int(response.timestamp * 1000)


def test_recorded_response_resolution_keeps_other_collective_lifecycles(
    archived_notification,
):
    from agent_comms.assignment_states import DeferredAssignment
    from agent_comms.coordination_cohort import accept_delivery_cohort
    from agent_comms.coordinator import Coordination

    old, _live, _source, _messages, root_id = archived_notification
    for name in ("sender", "reader", "agent"):
        old.channels.replace_tags(name, frozenset({"team"}))
    original = publish_initial(old, 
        "sender", "#team", "collective mixed response states"
    )
    with Coordination(str(old.root / "coordination.sqlite3")) as store:
        assignments = accept_delivery_cohort(
            old.bus, root_id, original.seq, store
        ).value.assignments
        delivered = assignments[0]
        paused = store.assignments.transition_preengagement(
            delivered.assignment_id,
            DeferredAssignment,
            expected_revision=delivered.revision,
        ).value
        store.assignments.transition_preengagement(
            delivered.assignment_id,
            DeliveredAssignment,
            expected_revision=paused.revision,
        )

    notifications = old.views.message_notifications_for_references(
        (original.reference,)
    )[original.seq, original.message_id]

    assert {notification.recipient for notification in notifications} == {
        "agent",
        "reader",
    }


def test_live_cli_ack_does_not_become_archived_ack(archived_notification):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.agent_activity import RecipientActivity
    from agent_comms.thread_execution import ExternalThreadExecution

    old, live, source, messages, _root_id = archived_notification
    old.registry.declare(replace(old.registry.require("agent"), execution=ExternalThreadExecution))
    unread = old.bus.inbox("agent")
    assert old.bus.mark_delivered(
        "agent", tuple(message.reference for message in unread)
    ) == 1
    current, = old.views.message_notifications_for_references((messages[0].reference,))[
        messages[0].seq, messages[0].message_id
    ]
    assert current.label == "Acknowledged" and not current.displayed_to
    with live.bus.history.read_sources() as sources:
        archived, = source.notification_references(sources, (messages[0].reference,))[
            messages[0].seq, messages[0].message_id
        ]
    assert archived.label == "Sent" and not archived.displayed_to
    assert FieldCodec.decode(RecipientActivity, FieldCodec.encode(RecordedRecipientActivity())) == RecordedRecipientActivity()
