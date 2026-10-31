"""Authored archived deliveries use recorded owners, not live availability."""


from agent_comms.notification_progress import AssignmentResponse, AnsweredResponse, UnansweredResponse
from agent_comms.notification_evidence import AllOutcomes
from agent_comms.repo import Checkout
from agent_comms.bus import Bus
from bus_record_fixture import prepare_native_comms

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
    IgnoredAssignment,
)
from agent_comms.channel_targets import CommandTarget
from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.comms import wire
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_tables.harness_deliveries import HarnessDelivery
from agent_comms.coordinator import Coordination
from agent_comms.harness_delivery_states import DeliveredOutcome, ObservedMonitor
from agent_comms.notification_progress import HarnessConsumedProgress
from agent_comms.coordination_errors import DMScopeChanged, SchemaVersionError
from agent_comms.nominal.field_codec import InputId, MessageId, TurnId
from transcript_record_fixture import transcript_record
from agent_comms.historical_views import HistoricalDisplay
from agent_comms.read_basis import ReadCursor
from agent_comms.read_ledger import ReadLedger
from agent_comms.thread_identity import AgentThreadRole, UserThreadRole
from agent_comms.wake_policy import FullWake
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.threads import Thread

SENDER = "sender"  # the notification carries its sender's lookup; these tests read progress only


@pytest.fixture
def archived_notification(tmp_path, *, thread_registration):
    old, live = (prepare_native_comms(wire(tmp_path / name)) for name in ("old", "live"))
    for comms in (old, live):
        comms.registry.declare(Thread('sender', frozenset(), Checkout(tmp_path), created_at=10.0))
        comms.registry.declare(Thread('reader', frozenset(), Checkout(tmp_path), created_at=11.0, role=AgentThreadRole if comms is old else UserThreadRole))
        comms.registry.declare(Thread('agent', frozenset(), Checkout(tmp_path), created_at=12.0 if comms is old else 212.0))
    root_id = old.registry.store.prepare_native_root().namespace
    messages = (
        publish_initial(old, "sender", "agent", "authored pending delivery"),
        publish_initial(old, "sender", "reader", "painted historical delivery"),
        publish_initial(old, "sender", "reader", "unpainted historical delivery"),
    )
    source = live.views.attach_history(old.root)
    return old, live, source, messages, root_id


def test_recorded_display_is_sparse_and_exact_incarnation(archived_notification):
    _old, live, source, messages, _root_id = archived_notification
    document = live.bus.reads.read()
    basis = live.bus.reads.capture(
        'reader', messages[1:], live.registry.snapshot(), Bus(Bus.path_in(Path(source.root))),
        document=document, conversation_snapshot=source.provenance)
    HistoricalDisplay(source, basis.select((messages[1].seq,))).acknowledge(live.bus.history, live.registry)
    with live.bus.history.read_sources() as sources:
        results = source.notification_references(sources, tuple(message.reference for message in messages[1:]),
                                               AllOutcomes())
    painted, = results.outcomes[messages[1].seq, messages[1].message_id]
    unpainted, = results.outcomes[messages[2].seq, messages[2].message_id]
    assert painted.displayed_to == (source.provenance.require("reader").incarnation,)
    assert unpainted.displayed_to == ()
    # A human display is Read and nothing else: no context entry rides along with it.
    assert painted.state == unpainted.state and not painted.state.consumed
    assert live.bus.reads.cursor("reader", live.registry.snapshot()) == ReadCursor()
    ledger = ReadLedger.at(source.root)
    document = ledger.read()
    reused = replace(basis, viewer_created_at=111.0)
    with pytest.raises(DMScopeChanged):
        HistoricalDisplay(source, reused).acknowledge(live.bus.history, live.registry)
    assert ledger.read() == document
    newer_namespace = replace(source.provenance, threads={
        **source.provenance.threads,
        "reader": replace(source.provenance.require("reader"), created_at=111.0),
    })
    assert ledger.historical_displayed_recipient(messages[1], painted.recipient_identity,
                                                newer_namespace, document=document) == ()


def test_harness_consumption_survives_human_display(archived_notification):
    """The human viewing an agent's conversation writes its own read, never the agent's receipt."""
    _old, live, _source, _messages, _root_id = archived_notification
    human = live.registry.require("reader")
    assert human.role is UserThreadRole
    # A Claude harness receives through a CLI bridge, so this case's agent is an external thread;
    # the UI then reads its dialogue through ThreadViewRead.external, not a native transcript.
    native = live.registry.require("agent")
    live.registry.register(replace(native, execution=ExternalThreadExecution))
    agent = live.registry.require("agent")
    assert agent.execution is ExternalThreadExecution and agent.role is AgentThreadRole
    assert agent.incarnation == native.incarnation
    # The human's own message to the agent is what the UI's human/agent dialogue page shows.
    shown = live.messaging.send_user_message(
        CommandTarget.named("agent"), "consumed, then displayed to the human", worktree=human.worktree)
    recipient, = live.bus.bus.delivery(shown.seq).recipients
    assert recipient.canonical_thread == "agent"
    with Coordination(str(CoordinationStore.path_in(live.root))) as store, \
            store.session.writing() as db:
        attempt = HarnessDelivery(ClaudeBridge, recipient.recipient_lookup, shown.message_id, shown.seq,
                                  DeliveredOutcome, ObservedMonitor(
                                      transcript_record('complete-original'), shown.timestamp))
        assert attempt.begin(store.assignments, db)
        attempt.record(store.assignments, db)

    def notification():
        current, = live.views.message_notifications_for_references(
            AllOutcomes(), (shown.reference,)).outcomes[shown.seq, shown.message_id]
        return current

    def human_display():
        return live.bus.reads.displayed_incarnation(
            shown, human.incarnation, live.registry.snapshot(), document=None)

    consumed = notification()
    assert consumed.state.consumed and not consumed.displayed_to
    assert consumed.label == consumed.timed_state('Consumed', shown.at_ms)
    ledger = live.bus.reads.read()
    assert human_display() == ()
    # The UI's "mark read" on the agent's thread, as the registered human viewer.
    live.views.mark_user_view_read("agent", worktree=human.worktree)
    assert live.bus.reads.read() != ledger
    assert human_display() == (human.incarnation,)
    displayed = notification()
    assert displayed.state == consumed.state and not displayed.displayed_to


def test_native_context_commit_is_consumed():
    from agent_comms.notification_assignment import AssignmentContextEvidence

    progress = AssignmentContextEvidence.from_columns(2_000).progress(pending_at_ms=1_000, queued=False)
    unshown = AssignmentContextEvidence.from_columns(None).progress(pending_at_ms=1_000, queued=False)
    assert progress.consumed and progress.at_ms == 2_000
    assert not unshown.consumed


def test_response_evidence_rejects_mixed_sql_columns():

    for values in ((1, None, 2), (None, "message-id", 2), (1, "message-id", None)):
        with pytest.raises(ValueError):
            AssignmentResponse.from_columns(*values)


def test_unshown_assignment_resolves_running_turn_as_timed_queue():
    from agent_comms.notification_assignment import (
        AssignmentActivityEvidence,
        NotShownContextEvidence,
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
        connection.execute("CREATE TABLE wake_claims (x INTEGER)")
    database.chmod(0o600)
    original = database.read_bytes()
    with pytest.raises(SchemaVersionError), live.bus.history.read_sources() as sources:
        source.notification_references(sources, (messages[0].reference,), AllOutcomes())
    assert database.read_bytes() == original


def test_recorded_engagement_is_not_an_active_turn(archived_notification):
    old, _live, _source, messages, root_id = archived_notification
    recipient, = old.bus.bus.delivery(messages[0].seq).recipients
    assignment = EngagedAssignment.build(FullWake(), "authored-execution", "sender")
    from agent_comms.notification_assignment import AssignmentActivity
    from agent_comms.notification_progress import ShownToModelProgress

    activity = AssignmentActivity(
        triage_inflight=False,
        progress=ShownToModelProgress(1),
        response=UnansweredResponse(),
    )
    with pytest.raises(TypeError):
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
    assert recorded.label.startswith("Consumed ") and not recorded.state.working
    assert recorded.state.detail == ""
    live = assignment.notification(
        recipient,
        SENDER,
        observation=UnavailableRecipientActivity(),
        activity=activity,
        updated_at_ms=1,
    )
    assert live.label.startswith("Consumed ") and not live.state.working
    assert live.state.detail == ""


def test_sent_recipient_progresses_through_durable_consumption_and_answer(archived_notification):
    old, _live, _source, messages, root_id = archived_notification
    recipient, = old.bus.bus.delivery(messages[0].seq).recipients
    from agent_comms.message_reference import MessageReference
    from agent_comms.notification_assignment import AssignmentActivity
    from agent_comms.activity import ActivityState, ObservedActivity, ReadyDrainReadiness
    from agent_comms.agent_activity import LiveRecipientActivity
    from agent_comms.notification_progress import (
        IgnoredProgress, QueuedProgress, SelectedConsumedProgress, SentProgress, ShownToModelProgress,
        TakenProgress, WorkingTakenProgress,
    )

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
    context_entry = ShownToModelProgress(2_000)
    consumed_activity = replace(sent_activity, progress=context_entry)
    consumed = engagement.notification(
        recipient,
        SENDER,
        observation=RecordedRecipientActivity(),
        activity=consumed_activity,
        updated_at_ms=1_000,
    )
    reply_id = MessageId.mint("durable-reply")
    answered_activity = replace(
        consumed_activity,
        response=AnsweredResponse(MessageReference(99, reply_id), 3_000),
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
            response=AnsweredResponse(MessageReference(99, reply_id), 3_000),
        ),
        updated_at_ms=2_000,
    )

    assert sent.label.startswith("Sent ") and sent.state == SentProgress(1_000)
    assert queued.label.startswith("Queued ") and queued.state == QueuedProgress(1_000)
    assert consumed.label.startswith("Consumed ") and consumed.state == SelectedConsumedProgress(2_000)
    assert answered.label.startswith("Answered ") and answered.answer == AnsweredResponse(MessageReference(99, reply_id), 3_000)
    # A delivered claim carries no consumed time of its own: without native or harness proof
    # its message is not consumed, whatever the claim's update time says. A running turn took
    # it; that is not a decision to ignore it.
    assert delivered.state == TakenProgress(SentProgress(1_000)) and not delivered.state.consumed
    assert delivered.label.startswith("Sent ")

    # Only the captured live activity says whether the recipient is working now.
    thread = old.registry.snapshot().threads[recipient.canonical_thread]
    def observed(state):
        return LiveRecipientActivity(thread, ObservedActivity(thread.name, state, readiness=ReadyDrainReadiness()))
    def delivered_to(observation, activity):
        return DeliveredAssignment.build(FullWake(), None, None).notification(
            recipient, SENDER, observation=observation, activity=activity, updated_at_ms=2_000)
    working = delivered_to(observed(ActivityState.WORKING), consumed_activity)
    assert working.state == WorkingTakenProgress(ShownToModelProgress(2_000))
    assert working.label.startswith("Seen, working ") and working.state.seen_working
    idle = delivered_to(observed(ActivityState.IDLE), consumed_activity)
    assert idle.label.startswith("Consumed ") and not idle.state.seen_working
    unconsumed = delivered_to(observed(ActivityState.WORKING), sent_activity)
    assert unconsumed.label.startswith("Sent ") and not unconsumed.state.seen_working
    confirmed = replace(unconsumed, state=unconsumed.state.model_consumed(HarnessConsumedProgress(context_entry.at_ms)))
    assert confirmed.state.seen_working and confirmed.state.consumed
    assert confirmed.label == confirmed.timed_state(WorkingTakenProgress.label, context_entry.at_ms)
    # An explicit ignore decision is the only Seen, not answering.
    ignored = IgnoredAssignment.build(FullWake(), None, None)
    declined = ignored.notification(recipient, SENDER, observation=RecordedRecipientActivity(),
                                    activity=consumed_activity, updated_at_ms=2_000)
    assert declined.label.startswith("Seen, not answering ") and declined.state.seen_not_answering
    unconfirmed = ignored.notification(recipient, SENDER, observation=RecordedRecipientActivity(),
                                       activity=sent_activity, updated_at_ms=2_000)
    assert unconfirmed.label == "Not answering" and not unconfirmed.state.seen_not_answering
    assert unconfirmed.state == IgnoredProgress(SentProgress(1_000))
    assert sum(row.state.consumed for row in (working, declined, unconfirmed, idle)) == 3
    assert not unconfirmed.state.consumed
    assert delivered_answer.label.startswith("Answered ")
    assert delivered_answer.answer == AnsweredResponse(MessageReference(99, reply_id), 3_000)
    assert sent.recipient_identity == consumed.recipient_identity == answered.recipient_identity


def test_delivered_assignment_uses_its_recorded_conversation_response(archived_notification):
    from agent_comms.assignment_states import DeferredAssignment
    from conftest import accept_initial
    from agent_comms.coordinator import Coordination

    old, _live, _source, messages, root_id = archived_notification
    original = messages[0]
    with Coordination(str(old.root / "coordination.sqlite3")) as store:
        assignment, = accept_initial(
            old.bus, root_id, original.seq, store
        )
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
    notification, = old.views.message_notifications_for_references(AllOutcomes(),
        (original.reference,)
    ).outcomes[original.seq, original.message_id]
    assert unrelated.answers == ()
    assert not notification.state.consumed
    assert notification.answer == UnansweredResponse()

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
        owner, TurnId('9ec9723c1d103dd9122ca0797881e236'), expected_generation=generation
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
    native_id = InputId(uuid4().hex)
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

    notification, = old.views.message_notifications_for_references(AllOutcomes(),
        (original.reference,)
    ).outcomes[original.seq, original.message_id]

    assert response.answers == (original.reference,)
    assert notification.label.startswith("Answered ")
    assert notification.answer == AnsweredResponse(response.reference, response.at_ms)


def test_recorded_response_resolution_keeps_other_collective_lifecycles(
    archived_notification,
):
    from agent_comms.assignment_states import DeferredAssignment
    from conftest import accept_initial
    from agent_comms.coordinator import Coordination

    old, _live, _source, _messages, root_id = archived_notification
    from conftest import declare_test_bus
    declare_test_bus(old, channels=("team",))
    for name in ("sender", "reader", "agent"):
        old.registry.register(
            replace(old.registry.require(name), tags=frozenset({"team"}))
        )
    original = publish_initial(old, 
        "sender", "#team", "collective mixed response states"
    )
    with Coordination(str(old.root / "coordination.sqlite3")) as store:
        assignments = accept_initial(
            old.bus, root_id, original.seq, store
        )
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

    notifications = old.views.message_notifications_for_references(AllOutcomes(),
        (original.reference,)
    ).outcomes[original.seq, original.message_id]

    assert {notification.recipient for notification in notifications} == {
        "agent",
        "reader",
    }


def test_live_cli_ack_does_not_become_archived_ack(archived_notification):
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.agent_activity import RecipientActivity
    from agent_comms.thread_execution import ExternalThreadExecution

    old, live, source, messages, _root_id = archived_notification
    old.registry.declare(replace(old.registry.require("agent"), execution=ExternalThreadExecution))
    unread = old.bus.inbox("agent").page.messages
    lookup = old.registry.snapshot().threads["agent"].incarnation.require_recorded().lookup
    # The bridge's own path: the attempt and the claim step it proves commit together.
    with Coordination(str(CoordinationStore.path_in(old.root))) as store, \
            store.session.writing() as db:
        for message in unread:
            attempt = HarnessDelivery(ClaudeBridge, lookup, message.message_id, message.seq,
                                      DeliveredOutcome, ObservedMonitor(
                                          transcript_record('complete-original'), message.timestamp))
            assert attempt.begin(store.assignments, db)
            attempt.record(store.assignments, db)
    current, = old.views.message_notifications_for_references(AllOutcomes(), (messages[0].reference,)).outcomes[
        messages[0].seq, messages[0].message_id
    ]
    assert current.state.consumed and not current.displayed_to
    assert current.label == current.timed_state('Consumed', messages[0].at_ms)
    with live.bus.history.read_sources() as sources:
        archived, = source.notification_references(sources, (messages[0].reference,), AllOutcomes()).outcomes[
            messages[0].seq, messages[0].message_id
        ]
    assert archived.label == "Sent" and not archived.displayed_to
    # The root-free recorded operation reads the same archive, not today's live evidence,
    # and a key that is no longer attached refuses instead of falling back to live reads.
    from agent_comms.coordination_errors import HistoricalSourceChanged
    from agent_comms.notification_reads import RecordedNotificationRead

    recorded, = RecordedNotificationRead((messages[0].reference,), source.key).read(live).outcomes[
        messages[0].seq, messages[0].message_id
    ]
    assert recorded == archived
    with pytest.raises(HistoricalSourceChanged):
        RecordedNotificationRead((messages[0].reference,), source.key + "-detached").read(live)
    assert FieldCodec.decode(RecipientActivity, FieldCodec.encode(RecordedRecipientActivity())) == RecordedRecipientActivity()
