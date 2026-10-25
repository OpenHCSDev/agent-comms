"""Real SQLite appends drive bounded wake scans and dependency reply reads."""

from dataclasses import replace

import pytest

from agent_comms.field_codec import AssignmentId, GoalId
from agent_comms.assignment_states import DeliveredAssignment, PendingDecision
from agent_comms.bus import BusRecord, PublicRecord
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.goal_presentation import GoalWaitTarget
from agent_comms.goal_waits import RecordedGoalWait
from agent_comms.input_drain import NoPrivateObservation, PrivateObservation
from agent_comms.locked_store import RegistryFile
from agent_comms.messages import InfoKind, Message, NoticeRole
from agent_comms.notification_assignment import (
    AnsweredResponse, AssignmentActivity, NotificationAssignment, UnansweredResponse,
)
from agent_comms.notification_progress import ShownToModelProgress
from agent_comms.read_basis import ChannelDisplayScope, ConversationCursor
from agent_comms.read_ledger import ReadLedger
from agent_comms.registration import Registration
from agent_comms.proven_source_coverage import SourcePrefix
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.wake_candidate_index import WakeCandidateIndex
from agent_comms.wake_policy import FullWake, MessageAudience
from bus_record_fixture import append_reply, record_from
from agent_comms.threads import Thread


@pytest.fixture
def owner():
    return RecordedThreadIncarnation("owner", 1000.0)


@pytest.fixture
def peer():
    return RecordedThreadIncarnation("peer", 1001.0)


@pytest.mark.parametrize("declaration", BusRecord.members_with(BusRecord))
def test_recipient_query_follows_frozen_delivery_membership(bus, declaration):
    record = bus.append(lambda seq, db: record_from(
        declaration, Message("author", "worker", "source", InfoKind, seq=seq)))
    assert bus.received("b" * 32, after=0, limit=1, descending=False) == record.deliveries()
    assert bus.received("b" * 32, after=record.seq, limit=1, descending=False) == ()
    assert bus.received("f" * 32, after=0, limit=1, descending=False) == ()


def test_three_committed_messages_are_candidates_by_their_bus_sequences(bus, owner, peer):
    messages = tuple(append_reply(bus, peer, owner, str(n)) for n in range(3))
    index = WakeCandidateIndex(bus)
    assert index.through_seq == messages[-1].seq
    page = index.page(recipient_lookup=owner.lookup,
                      after_seq=0, required_through_seq=messages[-1].seq)
    assert tuple(row.source_seq for row in page.entries) == tuple(row.seq for row in messages)
    assert tuple(row.message_id for row in page.entries) == tuple(row.message_id for row in messages)
    assert not page.has_more


def test_new_reader_observes_same_committed_candidates_without_catchup(bus, owner, peer):
    first = append_reply(bus, peer, owner)
    index = WakeCandidateIndex(bus)
    last = append_reply(bus, peer, owner)
    query = dict(recipient_lookup=owner.lookup,
                 after_seq=0, required_through_seq=last.seq)
    assert index.page(**query) == WakeCandidateIndex(bus).page(**query)
    assert tuple(row.source_seq for row in index.page(**query).entries) == (first.seq, last.seq)
    assert not index.page(**{**query, "recipient_lookup": peer.lookup}).entries


def test_committed_prefix_includes_non_candidate_suffix_without_another_writer(bus, owner, peer):
    first = append_reply(bus, peer, owner)
    public = tuple(bus.append(lambda seq, db: PublicRecord.from_message(
        Message("external", "owner", "public", InfoKind, seq=seq))) for _ in range(3))
    index = WakeCandidateIndex(bus)
    assert index.through_seq == public[-1].seq
    page = index.page(recipient_lookup=owner.lookup,
                      after_seq=0, required_through_seq=public[-1].seq)
    assert page.through_seq == public[-1].seq
    assert tuple(row.source_seq for row in page.entries) == (first.seq,)


def test_candidate_page_uses_bounded_recipient_keyset(bus, owner, peer):
    messages = tuple(append_reply(bus, peer, owner) for _ in range(3))
    query = dict(recipient_lookup=owner.lookup,
                 required_through_seq=messages[-1].seq, limit=2)
    index = WakeCandidateIndex(bus)
    first = index.page(after_seq=0, **query)
    assert tuple(row.source_seq for row in first.entries) == tuple(row.seq for row in messages[:2])
    assert first.has_more
    last = index.page(after_seq=first.entries[-1].source_seq, **query)
    assert tuple(row.source_seq for row in last.entries) == (messages[-1].seq,)
    assert not last.has_more


def test_startup_observation_is_unconditional_even_without_messages(bus, owner):
    assert NoPrivateObservation().has_unobserved_inputs(bus, owner.lookup)


def test_settled_observation_filters_committed_recipient_suffix(bus, peer):
    registry = Registration(RegistryFile.path_in(bus.path.parent))
    thread = registry.declare(Thread(
        "observed", frozenset(), str(bus.path.parent)))
    recipient = thread.incarnation.require_recorded()
    first = append_reply(bus, peer, recipient)
    with registry.store.reading() as document:
        entry = document.entry(thread.name)
    prefix = SourcePrefix.capture(bus, 0)
    observed = PrivateObservation(prefix, entry, ())
    assert not observed.has_unobserved_inputs(bus, recipient.lookup)
    append_reply(bus, peer, RecordedThreadIncarnation("elsewhere", 9000.0))
    assert not observed.has_unobserved_inputs(bus, recipient.lookup)
    append_reply(bus, peer, recipient)
    assert observed.has_unobserved_inputs(bus, recipient.lookup)
    assert prefix.through_seq == first.seq


def test_read_prefix_preserves_unpainted_holes_and_compacts_exact_reads(bus):
    messages = tuple(bus.append(lambda seq, db, target=target: PublicRecord.from_message(
        Message("sender", target, "visible", InfoKind, seq=seq)))
        for target in ("#team", "#elsewhere", "#team", "#team"))
    first, _, middle, last = messages
    scope = ChannelDisplayScope("#team", frozenset(("#team",)))
    assert ReadLedger._proved_read_prefix(bus, scope, ConversationCursor(), (last.seq,)) == 0
    assert ReadLedger._proved_read_prefix(
        bus, scope, ConversationCursor(), (first.seq, last.seq)) == middle.seq - 1
    assert ReadLedger._proved_read_prefix(
        bus, scope, ConversationCursor(middle.seq - 1, (last.seq,)), (middle.seq,)) == last.seq


def test_notification_answers_require_frozen_delivery_not_a_public_answer_reference(bus, owner, peer):
    original = append_reply(bus, peer, owner)
    assignment = WakeAssignment(
        assignment_id=AssignmentId("shown"), recipient=owner.name, recipient_lookup=owner.lookup,
        wire_seq=original.seq, message_id=original.message_id, audience=MessageAudience.DIRECT,
        lifecycle=DeliveredAssignment(PendingDecision(FullWake())),
        accepted_at_ms=1, updated_at_ms=1)
    row = NotificationAssignment(assignment, AssignmentActivity(
        False, ShownToModelProgress(1), UnansweredResponse()))
    bus.append(lambda seq, db: PublicRecord.from_message(Message(
        owner.name, peer.name, "public answer is not frozen delivery evidence", InfoKind,
        seq=seq, answers=(original.reference,))))
    unanswered, = NotificationAssignment._recorded_conversation_responses(
        bus.path.parent, (original,), (row,))
    assert isinstance(unanswered.activity.response, UnansweredResponse)
    reply = append_reply(bus, owner, peer, answers=(original.reference,))
    answered, = NotificationAssignment._recorded_conversation_responses(
        bus.path.parent, (original,), (row,))
    assert answered.activity.response == AnsweredResponse(
        reply.reference, int(reply.timestamp * 1000))


def test_wait_finds_dependency_reply_after_its_recorded_sequence(bus, owner, peer):
    goal_id = GoalId.mint()
    old = append_reply(bus, peer, owner, "before wait")
    wait = RecordedGoalWait(
        goal_id=goal_id, wait_id="wait", revision=0, after_seq=old.seq,
        targets=(GoalWaitTarget(peer),), owner=owner, target_turn_generations=(None,),
        report_turn_id=None, report_turn_generation=None)
    assert not wait.has_reply(bus, owner)
    bus.append(lambda seq, db: PublicRecord.from_message(
        Message("other", owner.name, "not a private dependency reply", InfoKind, seq=seq)))
    append_reply(bus, peer, owner, "notice is not a dependency reply", role=NoticeRole())
    append_reply(bus, RecordedThreadIncarnation("other", 1002.0), owner, "unrelated private reply")
    assert not wait.has_reply(bus, owner)
    renamed_owner = replace(owner, name="renamed-owner")
    reply = append_reply(bus, peer, renamed_owner)
    assert wait.has_reply(bus, renamed_owner)
    assert not replace(wait, after_seq=reply.seq).has_reply(bus, owner)
    assert not replace(wait, targets=(GoalWaitTarget(
        RecordedThreadIncarnation(peer.name, 2000.0)),)).has_reply(bus, owner)
