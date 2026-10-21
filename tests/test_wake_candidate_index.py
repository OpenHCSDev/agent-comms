"""Real SQLite appends drive bounded wake scans and dependency reply reads."""

from dataclasses import replace

import pytest

from agent_comms.audience_manifest import FrozenRecipient, freeze_audience
from agent_comms.bus import Bus, PrivateRecord, PublicRecord
from agent_comms.goal_presentation import GoalWaitTarget
from agent_comms.goal_waits import RecordedGoalWait
from agent_comms.messages import InfoKind, Message
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.wake import ControlClassification, resolve_wake_cohort
from agent_comms.wake_candidate_index import CommittedAppendHint, WakeCandidateIndex


@pytest.fixture
def bus(tmp_path):
    return Bus(Bus.path_in(tmp_path))


@pytest.fixture
def owner():
    return RecordedThreadIncarnation("owner", 1000.0)


@pytest.fixture
def peer():
    return RecordedThreadIncarnation("peer", 1001.0)


def append_reply(bus, sender, recipient, body="reply"):
    def build(seq):
        message = Message(sender.name, recipient.name, body, InfoKind, seq=seq)
        audience = freeze_audience(
            message, (FrozenRecipient(recipient.lookup, recipient.name),), "c" * 64,
            sender_lookup=sender.lookup, sender_name=sender.name)
        return PrivateRecord.of(
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
    public = tuple(bus.append(lambda seq: PublicRecord.of(
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


def test_wait_finds_dependency_reply_after_its_recorded_sequence(bus, owner, peer):
    old = append_reply(bus, peer, owner, "before wait")
    wait = RecordedGoalWait(
        goal_id="goal", wait_id="wait", revision=0, after_seq=old.seq,
        targets=(GoalWaitTarget(peer),), owner=owner, target_turn_generations=(None,),
        report_turn_id=None, report_turn_generation=None)
    assert not wait.has_reply(bus, owner)
    bus.append(lambda seq: PublicRecord.of(
        Message("other", owner.name, "not a private dependency reply", InfoKind, seq=seq)))
    reply = append_reply(bus, peer, owner)
    assert wait.has_reply(bus, owner)
    assert not replace(wait, after_seq=reply.seq).has_reply(bus, owner)
    assert not replace(wait, targets=(GoalWaitTarget(
        RecordedThreadIncarnation(peer.name, 2000.0)),)).has_reply(bus, owner)
