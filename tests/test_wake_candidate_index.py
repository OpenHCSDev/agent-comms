"""Real SQLite appends drive bounded wake scans and dependency reply reads."""

from dataclasses import replace

import pytest

from agent_comms.audience_manifest import FrozenRecipient, freeze_audience
from agent_comms.assignment_states import DeliveredAssignment, PendingDecision
from agent_comms.bus import Bus, PrivateRecord, PublicRecord
from agent_comms.candidate_maintenance import drain_candidate_catchup, schedule_candidate_catchup
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.goal_presentation import GoalWaitTarget
from agent_comms.goal_waits import RecordedGoalWait
from agent_comms.messages import ConversationRole, InfoKind, Message, NoticeRole
from agent_comms.notification_assignment import (
    AnsweredResponse, AssignmentActivity, NotificationAssignment, UnansweredResponse,
)
from agent_comms.notification_progress import ShownToModelProgress
from agent_comms.read_basis import ChannelDisplayScope, ConversationCursor
from agent_comms.read_ledger import ReadLedger
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.wake import ControlClassification, resolve_wake_cohort
from agent_comms.wake_candidate_index import CommittedAppendHint, WakeCandidateIndex
from agent_comms.wake_policy import FullWake, MessageAudience


@pytest.fixture
def bus(tmp_path):
    return Bus(Bus.path_in(tmp_path))


@pytest.fixture
def owner():
    return RecordedThreadIncarnation("owner", 1000.0)


@pytest.fixture
def peer():
    return RecordedThreadIncarnation("peer", 1001.0)


def append_reply(bus, sender, recipient, body="reply", *, role=ConversationRole(), answers=()):
    def build(seq, db):
        message = Message(sender.name, recipient.name, body, InfoKind, seq=seq, role=role,
                          answers=answers)
        audience = freeze_audience(
            message, (FrozenRecipient(recipient.lookup, recipient.name),), "c" * 64,
            sender_lookup=sender.lookup, sender_name=sender.name)
        return PrivateRecord.from_message(
            message, audience=audience,
            decisions=resolve_wake_cohort(
                message, frozen_audience=audience, control=ControlClassification.ORDINARY),
            control=ControlClassification.ORDINARY.value)
    return bus.append(build)


def test_three_committed_messages_are_candidates_by_their_bus_sequences(bus, owner, peer):
    messages = tuple(append_reply(bus, peer, owner, str(n)) for n in range(3))
    index = WakeCandidateIndex(bus)
    assert index.maintain() == messages[-1].seq
    page = index.page(root_id="a" * 32, recipient_lookup=owner.lookup,
                      after_seq=0, required_through_seq=messages[-1].seq)
    assert tuple(row.source_seq for row in page.entries) == tuple(row.seq for row in messages)
    assert tuple(row.message_id for row in page.entries) == tuple(row.message_id for row in messages)
    assert not page.has_more


def test_rebuild_recreates_the_same_candidate_rows(bus, owner, peer):
    messages = tuple(append_reply(bus, peer, owner, str(n)) for n in range(3))
    index = WakeCandidateIndex(bus)
    index.maintain()
    query = dict(root_id="a" * 32, recipient_lookup=owner.lookup,
                 after_seq=0, required_through_seq=messages[-1].seq)
    before = index.page(**query)
    assert index.rebuild() == messages[-1].seq
    assert index.page(**query) == before


def test_scan_progress_includes_non_candidate_suffix_and_is_bounded(bus, owner, peer):
    first = append_reply(bus, peer, owner)
    public = tuple(bus.append(lambda seq, db: PublicRecord.from_message(
        Message("external", "owner", "public", InfoKind, seq=seq))) for _ in range(3))
    index = WakeCandidateIndex(bus)
    hint = CommittedAppendHint.capture("a" * 32, public[-1].seq)
    assert not index.catch_up_committed_append(hint, max_rows=2)
    assert index.through_seq == public[0].seq
    assert index.catch_up_committed_append(hint, max_rows=2)
    assert index.maintain() == public[-1].seq
    page = index.page(root_id=hint.root_id, recipient_lookup=owner.lookup,
                      after_seq=0, required_through_seq=hint.seq)
    assert tuple(row.source_seq for row in page.entries) == (first.seq,)


def test_background_maintenance_coalesces_committed_sequences(bus, owner, peer):
    first = append_reply(bus, peer, owner)
    schedule_candidate_catchup(bus, first.seq)
    last = append_reply(bus, peer, owner)
    schedule_candidate_catchup(bus, last.seq)
    drain_candidate_catchup(bus.path.parent)
    index = WakeCandidateIndex(bus)
    assert index.through_seq == last.seq
    assert tuple(row.source_seq for row in index.page(
        root_id="a" * 32, recipient_lookup=owner.lookup, after_seq=0,
        required_through_seq=last.seq).entries) == (first.seq, last.seq)


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
        assignment_id="shown", recipient=owner.name, recipient_lookup=owner.lookup,
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
    old = append_reply(bus, peer, owner, "before wait")
    wait = RecordedGoalWait(
        goal_id="goal", wait_id="wait", revision=0, after_seq=old.seq,
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
