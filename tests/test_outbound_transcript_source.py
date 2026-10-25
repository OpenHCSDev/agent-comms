"""Whole conversations derive their original frozen wire sources, in both directions."""

from agent_comms.channel_targets import CommandTarget

import sqlite3
from dataclasses import replace

import pytest

from agent_comms.channels import AnyChannel
from agent_comms.comms import wire
from agent_comms.coordination_errors import StaleRevision
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.notification_assignment import NotificationAssignment
from agent_comms.notification_progress import (
    ContextualProgress,
    SentProgress,
    UnrecordedProgress,
)
from agent_comms.pi_payloads import PublishedMessageToolDetails
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import invoke_tool
from agent_comms.transcripts import TranscriptRead
from test_coordinated_runtime import _root, tmp_path  # noqa: F401
from agent_comms.threads import Thread


def test_original_read_contention_is_unavailable_then_same_receipts_return(tmp_path):  # noqa: F811
    from agent_comms.coordination_errors import CoordinationReadUnavailable

    root, _root_id, comms, initial, _people = _root(tmp_path)
    database = root / "coordination.sqlite3"
    captured = NotificationAssignment.read(root, (initial,))
    original = captured.resolve(root)
    wire_before = comms.bus.bus.revision()
    assert original
    with Coordination(str(database)) as writer:
        with writer.session.writing():
            with pytest.raises(CoordinationReadUnavailable) as unavailable:
                NotificationAssignment.read(root, (initial,))
            assert unavailable.value.__cause__.sqlite_errorcode & 0xFF in (
                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
            )
            with pytest.raises(CoordinationReadUnavailable):
                with TranscriptRead.publication_source(root):
                    pytest.fail("Busy acquisition reached the native schema decoder")
    reopened = NotificationAssignment.read(root, (initial,))
    assert reopened == captured
    assert reopened.resolve(root) == original
    with TranscriptRead.publication_source(root) as native_read:
        assert native_read is not None
    assert comms.bus.bus.revision() == wire_before


@pytest.mark.parametrize('direct', [False, True])
def test_saved_notification_does_not_mix_owner_drain_readiness(tmp_path, direct):  # noqa: F811
    from agent_comms.activity import StoppedDrainDiagnostic

    _path, _root_id, comms, initial, _people = _root(tmp_path, direct=direct)
    owner = comms.registry.snapshot().owner_identity('beta')
    diagnostic = StoppedDrainDiagnostic(
        owner, 'NativePiUnavailable', 'Original outcome uncertain', recorded_at=1.0
    )
    assert comms.agents.set_drain_diagnostic('beta', owner, diagnostic)
    notice = next(item for item in comms.views.thread_presentations(('sender',)).require('sender').notifications
                  if item.recipient=='beta')
    assert type(notice.state) is SentProgress
    assert notice.state.detail == ''
    assert notice.message.reference==initial.message.reference
    assert not notice.state.working
    # A diagnostic from another owner generation cannot block a current
    # participant; neither its text nor a running PID grants that relation.
    foreign = replace(diagnostic,owner=replace(owner,generation=owner.generation+1))
    activity = comms.agents.activity_of('beta', snapshot=comms.registry.snapshot()).source_event()
    comms.agents.activity.emit(replace(activity,diagnostic=foreign))
    current = next(item for item in comms.views.thread_presentations(('sender',)).require('sender').notifications
                   if item.recipient=='beta')
    assert type(current.state) is SentProgress


def test_frozen_unhandled_delivery_projects_recovery_without_claiming(tmp_path):  # noqa: F811
    from agent_comms.activity import StoppedDrainDiagnostic
    from agent_comms.coordination_tables.assignments import WakeAssignment

    root, _root_id, comms, _initial, _people = _root(tmp_path)
    owner = comms.registry.snapshot().owner_identity('beta')
    diagnostic = StoppedDrainDiagnostic(
        owner, 'NativePiUnavailable', 'Original uncertain outcome', recorded_at=1.0
    )
    assert comms.agents.set_drain_diagnostic('beta', owner, diagnostic)
    message = comms.messaging.send_user_message(CommandTarget.named('#team'), 'Independent saved original',
                                               worktree=str(tmp_path))
    with Coordination(str(root/'coordination.sqlite3')) as store:
        before = WakeAssignment.select(store.session._connection)
    wire_before = comms.bus.bus.revision()
    notices = comms.views.message_notifications_for_references((message.reference,))[message.seq,message.message_id]
    beta = next(notice for notice in notices if notice.recipient=='beta')
    assert beta.state == ContextualProgress(UnrecordedProgress())
    assert beta.state.detail == ''
    assert beta.recipient_identity.recipient_lookup==comms.registry.require('beta').incarnation.require_recorded().lookup
    with Coordination(str(root/'coordination.sqlite3')) as store:
        assert WakeAssignment.select(store.session._connection)==before
    assert not any(row.wire_seq==message.seq for row in before)
    assert comms.bus.bus.revision()==wire_before
    # The immutable source's recipient survives rename; current readiness must
    # join its original birth rather than manufacture a new membership/claim.
    comms.registry.rename('beta','renamed')
    renamed = comms.views.message_notifications_for_references((message.reference,))[message.seq,message.message_id]
    assert {item.recipient_identity for item in renamed}=={item.recipient_identity for item in notices}


def test_capture_page_window_names_zero_and_nonzero_sent_windows(tmp_path):  # noqa: F811
    from agent_comms.transcripts import TranscriptSourceWindow

    _path, _root_id, comms, initial, _people = _root(tmp_path)
    ordinary = comms.transcripts.capture_page_window("sender")
    assert isinstance(ordinary, TranscriptSourceWindow)
    assert ordinary.sent_sources == ()
    with_sent = comms.transcripts.capture_page_window("sender", sent_source_limit=5)
    assert isinstance(with_sent, TranscriptSourceWindow)
    assert [source.message.reference for source in with_sent.sent_sources] == [
        initial.message.reference
    ]


def test_notification_window_preserves_exact_receipts_and_rejects_duplicates(tmp_path):  # noqa: F811
    root, _root_id, comms, initial, _people = _root(tmp_path)
    records = NotificationAssignment.read(root, (initial,)).resolve(root)
    assert records
    projected = tuple(NotificationAssignment.for_deliveries((initial,), records))
    assert projected[0][0] is initial
    assert any(outcome is records[0] for outcome in projected[0][1])
    with pytest.raises(ValueError):
        tuple(NotificationAssignment.for_deliveries((initial,), (*records, records[0])))
    # Duplicate evidences outside the acquired deliveries do not change their result.
    assert tuple(NotificationAssignment.for_deliveries((), (*records, records[0]))) == ()


def test_publication_intent_joins_original_sender_and_target_only(tmp_path):  # noqa: F811
    from agent_comms.field_codec import FieldCodec
    from agent_comms.routing import MessageRoute, TurnRouting
    from agent_comms.transcript_sources import AssignedTranscriptSource

    _path, _root_id, comms, initial, _people = _root(tmp_path)
    reply = comms.messaging.send("beta", "sender", "First original reply").message
    source = AssignedTranscriptSource.for_thread(
        comms.root, comms.registry.require("beta"), comms.bus.bus
    )
    original = source.rows(sequences=(reply.seq,))
    lookup = source.recipient.require_recorded().lookup
    intent = TurnRouting((initial.message.reference,), MessageRoute("beta", ("sender", "alpha")))
    # A later send can fail; this first committed subset stays a valid relation.
    intent.require_publications(original, lookup)
    absent = TurnRouting()
    assert not absent.outgoing
    assert FieldCodec.encode(absent)["reply"] is None
    assert TurnRouting.from_wire(absent.to_wire()) == absent
    absent.require_publications((), lookup)
    with pytest.raises(RelationViolationError):
        absent.require_publications(original, lookup)
    reference_only = FieldCodec.encode(intent)["requests"]
    assert reference_only == [FieldCodec.encode(initial.message.reference)]
    assert TurnRouting.from_wire(intent.to_wire()) == intent
    with pytest.raises((TypeError, ValueError)):
        TurnRouting.from_wire({"requests": [initial.message.to_wire()], "reply": None})
    with pytest.raises(RelationViolationError):
        intent.require_publications(original, "foreign-incarnation")
    with pytest.raises(RelationViolationError):
        replace(intent, reply=MessageRoute("beta", ("alpha",))).require_publications(original, lookup)
    with pytest.raises(RelationViolationError):
        replace(intent, publications=(reply.reference,)).require_publications(original, lookup)


def test_open_sender_source_advances_without_a_native_tool_copy(tmp_path):  # noqa: F811
    _path, _root_id, comms, initial, _people = _root(tmp_path)
    before = comms.transcripts.capture_page_read("beta")
    acknowledgement = PublishedMessageToolDetails.from_wire(invoke_tool(
        comms, "comms_send",
        {"to": "sender", "body": "Original outgoing ownership reply"},
        caller=ThreadCaller("beta"),
    ))
    message, = comms.bus.bus.messages_for_references((acknowledgement.message.source,))
    after = comms.transcripts.capture_page_read("beta")
    assert after.identity.wire_frontier.sequence == message.seq
    assert not before.current()
    with pytest.raises(StaleRevision):
        before.read()
    events = after.read().events
    assert [event.text for event in events] == [initial.message.body, message.body]
    assert [event.timestamp for event in events] == [initial.message.timestamp, message.timestamp]
    sent = events[-1]
    assert sent.declared_name == "sent"
    assert sent.source == message.reference
    assert sent.routing.reply.targets == (message.target,)
    # The reader's name changes; the original messages and sent source do not.
    comms.registry.rename("beta", "renamed")
    reopened = wire(comms.root).transcripts.capture_page_read("renamed").read()
    assert reopened.events == (replace(events[0], reader="renamed"), events[1])
    assert list(comms.views.history_page(AnyChannel.target()).messages)[-1] == message


def test_unrelated_canonical_append_keeps_open_source_content(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    before = comms.transcripts.capture_page_read("beta")
    page = before.read()
    comms.messaging.send("sender", "alpha", "Unrelated original").message
    after = comms.transcripts.capture_page_read("beta")
    assert before.identity.bus_revision != after.identity.bus_revision
    assert before.identity.wire_frontier == after.identity.wire_frontier
    assert not before.current()
    assert before.content_current()
    assert before.read() == page
    assert before.identity.same_content(after.identity)
    comms.messaging.send("sender", "beta", "Related original").message
    assert not before.content_current()


def test_open_source_rejects_replaced_wire_inode(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    before = comms.transcripts.capture_page_read("beta")
    replacement = comms.bus.bus.path.with_suffix(".replacement")
    comms.bus.bus.backup(replacement)
    replacement.replace(comms.bus.bus.path)
    assert not before.content_current()
    with pytest.raises(StaleRevision):
        before.read()


def test_published_source_binding_keeps_exact_original_request(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    identity = comms.views.thread_presentations(("beta",)).require("beta").read_identity
    assert comms.transcripts.bind_page_read("beta", identity).identity is identity
    with pytest.raises(StaleRevision):
        comms.transcripts.bind_page_read("alpha", identity)
    with pytest.raises(StaleRevision):
        wire(tmp_path / "other").transcripts.bind_page_read("beta", identity)
    with pytest.raises(StaleRevision):
        comms.transcripts.bind_page_read(
            "beta", identity,
            before=identity.page_bound.at_offset(0),
        )
    comms.registry.rename("beta", "renamed")
    assert comms.transcripts.bind_page_read("renamed", identity).identity is identity
    comms.registry.unregister("renamed")
    comms.registry.remove("renamed")
    comms.registry.declare(Thread('renamed', frozenset(), str(tmp_path)))
    with pytest.raises(StaleRevision):
        comms.transcripts.bind_page_read("renamed", identity)


def test_missing_bus_cannot_be_an_empty_presentation(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    comms.bus.bus.path.unlink()
    with pytest.raises(sqlite3.OperationalError) as missing:
        comms.views.thread_presentations(("beta",)).require("beta")
    assert missing.value.sqlite_errorcode == sqlite3.SQLITE_CANTOPEN
