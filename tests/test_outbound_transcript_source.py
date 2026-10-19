"""Whole conversations derive their original frozen wire sources, in both directions."""

from agent_comms.channels import ANY_CHANNEL
from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
from agent_comms.tools import invoke_tool
from agent_comms.tool_caller import NoThreadCaller
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.comms import wire
from agent_comms.coordination_errors import StaleRevision
from agent_comms.assignment_states import IgnoredAssignment
from agent_comms.coordinator import Coordination
from agent_comms.notification_assignment import NotificationAssignment
from agent_comms.notification_progress import (
    ContextualProgress, IgnoredProgress, SentProgress, ShownToModelProgress, UnrecordedProgress,
)
from agent_comms.errors import RelationViolationError
from agent_comms.message_reference import MessageReference
from agent_comms.presentation import MessageNotification
from agent_comms.revision_views import StoresView
from agent_comms.pi_payloads import PublishedMessageToolDetails
from agent_comms.wire_log import WireLog, WireStoreLock
from agent_comms.threads import Thread
from agent_comms.transcripts import TranscriptCursor, TranscriptRead
from test_coordinated_runtime import _root, tmp_path  # noqa: F401

import pytest


def test_original_read_contention_is_unavailable_then_same_receipts_return(tmp_path):  # noqa: F811
    import sqlite3
    from agent_comms.coordination_errors import CoordinationReadUnavailable

    root, _root_id, comms, initial, _people = _root(tmp_path)
    database = root / "coordination.sqlite3"
    captured = NotificationAssignment.read(root, (initial,))
    original = captured.resolve(root)
    wire_before = comms.bus.log.path.read_bytes()
    assert original
    with Coordination(str(database)) as writer:
        with writer.session.transaction():
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
    assert comms.bus.log.path.read_bytes() == wire_before


@pytest.mark.parametrize('direct', [False, True])
def test_saved_notification_does_not_mix_owner_drain_readiness(tmp_path, direct):  # noqa: F811
    from dataclasses import replace
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
    message = comms.messaging.send_user_message('#team', 'Independent saved original',
                                               worktree=str(tmp_path))
    with Coordination(str(root/'coordination.sqlite3')) as store:
        before = WakeAssignment.select(store.session._connection)
    wire_before = comms.bus.log.path.read_bytes()
    notices = comms.views.message_notifications_for_references((message.reference,))[message.seq,message.message_id]
    beta = next(notice for notice in notices if notice.recipient=='beta')
    assert beta.state == ContextualProgress(UnrecordedProgress())
    assert beta.state.detail == ''
    assert beta.recipient_identity.recipient_lookup==RecordedThreadIncarnation("fixture", comms.registry.require('beta').created_at).lookup
    with Coordination(str(root/'coordination.sqlite3')) as store:
        assert WakeAssignment.select(store.session._connection)==before
    assert not any(row.wire_seq==message.seq for row in before)
    assert comms.bus.log.path.read_bytes()==wire_before
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
    with pytest.raises(ValueError, match="multiple handling rows"):
        tuple(NotificationAssignment.for_deliveries((initial,), (*records, records[0])))
    # Duplicate evidences outside the acquired deliveries do not change their result.
    assert tuple(NotificationAssignment.for_deliveries((), (*records, records[0]))) == ()


def test_publication_intent_joins_original_sender_and_target_only(tmp_path):  # noqa: F811
    from dataclasses import replace
    from agent_comms.field_codec import FieldCodec
    from agent_comms.routing import MessageRoute, TurnRouting
    from agent_comms.transcript_sources import AssignedTranscriptSource

    _path, _root_id, comms, initial, _people = _root(tmp_path)
    reply = comms.messaging.send("beta", "sender", "First original reply").message
    source = AssignedTranscriptSource.for_thread(
        comms.root, comms.registry.require("beta"), comms.bus.log
    )
    original = tuple(source.rows("w.seq=?", (reply.seq,)))
    lookup = RecordedThreadIncarnation("fixture", source.recipient.created_at).lookup
    intent = TurnRouting((initial.message.reference,), MessageRoute("beta", ("sender", "alpha")))
    # A later send can fail; this first committed subset stays a valid relation.
    intent.require_publications(original, lookup)
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
        {"from": "beta", "to": "sender", "body": "Original outgoing ownership reply"},
        caller=NoThreadCaller(),
    ))
    message, = comms.bus.log.messages_for_references((acknowledgement.message.source,))
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
    from dataclasses import replace
    comms.registry.rename("beta", "renamed")
    reopened = wire(comms.root).transcripts.capture_page_read("renamed").read()
    assert reopened.events == (replace(events[0], reader="renamed"), events[1])
    assert list(comms.bus.history_page(ANY_CHANNEL).messages)[-1] == message


async def test_original_target_handling_revokes_open_sender_read_without_new_message(tmp_path, monkeypatch, *, native_runtime_package):  # noqa: F811
    from agent_comms.coordinated_runtime import SelectedExecution
    from agent_comms.native_runtime_input import NativeRuntimeInput
    from test_coordinated_runtime import _fake_model

    root, root_id, comms, initial, _people = _root(tmp_path)
    message = initial.message
    before = comms.transcripts.capture_page_read("sender")
    sender_page = before.read()
    target = comms.registry.require("beta")
    lookup = RecordedThreadIncarnation("fixture", target.created_at).lookup
    receipts = comms.views.thread_presentations(("sender",)).require("sender").notifications
    beta = next(item for item in receipts if item.recipient_identity.recipient_lookup == lookup)
    assert beta.message.reference == message.reference
    assert type(beta.state) is SentProgress
    outcomes = comms.views.message_notifications_for_references((message.reference,))[
        message.seq, message.message_id
    ]
    with pytest.raises(RelationViolationError):
        comms.views.message_notifications_for_references(
            (MessageReference(message.seq + 1, message.message_id),)
        )
    assert comms.views.message_notifications_for_references(
        (message.reference,) * (MessageNotification.window_limit + 1)
    ) == {(message.seq, message.message_id): outcomes}
    # Both views are already open. Later messages move this original beyond the
    # bounded recent-notification window before its handling changes.
    for index in range(6):
        invoke_tool(
            comms,
            "comms_send",
            {
                "from": "sender",
                "to": "beta",
                "body": f"Later original {index}",
            }, caller=NoThreadCaller(),
        )
    open_sender = comms.views.thread_presentations(("sender",)).require("sender")
    open_recipient = comms.views.thread_presentations(("beta",)).require("beta")
    assert all(item.message.reference != message.reference for item in open_sender.notifications)
    before = comms.transcripts.capture_page_read("sender")
    sender_page = before.read()
    wire_before = comms.bus.log.path.read_bytes()
    published_revision = comms.views.revision()
    runner, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", runner)
    outcome = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=native_runtime_package,
    ).run()
    assert outcome is not None and outcome.disposition is IgnoredAssignment
    assert len(calls) == 1
    assert comms.views.revision().changed_since(StoresView(), published_revision)
    after = comms.transcripts.capture_page_read("sender")
    assert not before.current()
    assert before.content_current()
    assert before.read() == sender_page
    assert after.identity.wire_frontier == before.identity.wire_frontier
    assert after.read() == sender_page
    assert comms.bus.log.path.read_bytes() == wire_before
    sender = comms.views.thread_presentations(("sender",)).require("sender")
    recipient = comms.views.thread_presentations(("beta",)).require("beta")
    assert sender.notifications == open_sender.notifications
    assert recipient.notifications == open_recipient.notifications
    assert sender.read_identity != open_sender.read_identity
    assert recipient.read_identity != open_recipient.read_identity
    assert sender.read_identity == after.identity
    outcomes = comms.views.message_notifications_for_references((message.reference,))[
        message.seq, message.message_id
    ]
    target_outcome = next(
        item for item in outcomes if item.recipient_identity.recipient_lookup == lookup
    )
    with Coordination(str(root / "coordination.sqlite3")) as store:
        native, = NativeRuntimeInput.select(
            store.session._connection, where="input_id=?", parameters=(outcome.input_id,)
        )
        context = native.require_context_proof()
    assert target_outcome.state == IgnoredProgress(
        ShownToModelProgress(context.committed_at_ms)
    )
    assert target_outcome.state.read and target_outcome.state.delivered
    assert target_outcome.state.detail == ""


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
    replacement = comms.bus.log.path.with_suffix(".replacement")
    replacement.write_bytes(comms.bus.log.path.read_bytes())
    replacement.replace(comms.bus.log.path)
    with pytest.raises(RelationViolationError):
        before.content_current()


def test_open_thread_shares_original_notification_and_frontier_read(tmp_path, monkeypatch):  # noqa: F811
    _path, _root_id, comms, initial, _people = _root(tmp_path)
    verify = WireStoreLock.open_source
    barriers = []

    def observed(cls, path, mode):
        if path == comms.bus.log.path:
            barriers.append(True)
        return verify(path, mode)

    monkeypatch.setattr(WireStoreLock, "open_source", classmethod(observed))
    view = comms.views.thread_presentations(("beta",)).require("beta")
    assert len(barriers) == 1
    assert view.read_identity.wire_frontier.sequence == initial.message.seq
    assert view.notifications[0].message.reference == initial.message.reference
    assert view.notifications[0].recipient_identity.recipient_lookup == RecordedThreadIncarnation("fixture", view.read_identity.thread.created_at).lookup


def test_published_source_binding_keeps_exact_original_request(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    identity = comms.views.thread_presentations(("beta",)).require("beta").read_identity
    assert comms.transcripts.bind_page_read("beta", identity).identity is identity
    with pytest.raises(StaleRevision, match="original thread"):
        comms.transcripts.bind_page_read("alpha", identity)
    with pytest.raises(StaleRevision, match="another root"):
        wire(tmp_path / "other").transcripts.bind_page_read("beta", identity)
    with pytest.raises(StaleRevision, match="page window"):
        comms.transcripts.bind_page_read(
            "beta", identity,
            before=identity.runtime.cursor(identity.wire_frontier, 0),
        )
    comms.registry.rename("beta", "renamed")
    assert comms.transcripts.bind_page_read("renamed", identity).identity is identity
    comms.registry.unregister("renamed")
    comms.registry.remove("renamed")
    comms.registry.declare(comms.registry.new_thread('renamed', frozenset(), str(tmp_path)))
    with pytest.raises(StaleRevision, match="original thread"):
        comms.transcripts.bind_page_read("renamed", identity)


def test_original_page_reuses_captured_frontier_between_claim_fences(tmp_path, monkeypatch):  # noqa: F811
    _path, _root_id, comms, initial, _people = _root(tmp_path)
    read = comms.transcripts.capture_page_read("beta")
    verify = WireStoreLock.open_source
    barriers = []

    def observed(cls, path, mode):
        if path == comms.bus.log.path:
            barriers.append(True)
        return verify(path, mode)

    monkeypatch.setattr(WireStoreLock, "open_source", classmethod(observed))
    page = read.read()
    # One bounded page query and one final freshness check. The retained source
    # removes the former pre-read identity reconstruction.
    assert len(barriers) == 2
    assert page.after.wire == read.identity.wire_frontier
    assert tuple(event.source for event in page.events) == (initial.message.reference,)


def test_original_page_rejects_relevant_append_during_preparation(tmp_path, monkeypatch):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    read = comms.transcripts.capture_page_read("beta")
    prepare = comms.transcripts._thread_transcript_page

    def append_after_page(*args, **kwargs):
        page = prepare(*args, **kwargs)
        comms.messaging.send("sender", "beta", "Original concurrent append").message
        return page

    monkeypatch.setattr(comms.transcripts, "_thread_transcript_page", append_after_page)
    with pytest.raises(StaleRevision, match="during preparation"):
        read.read()


def test_original_window_uses_the_barriers_open_certificate(tmp_path, monkeypatch):  # noqa: F811
    import agent_comms.private_bus_checkpoint as checkpoint

    _path, _root_id, comms, initial, _people = _root(tmp_path)
    saved = checkpoint._saved
    reads = []

    def observed(connection):
        reads.append(True)
        return saved(connection)

    monkeypatch.setattr(checkpoint, "_saved", observed)
    view = comms.views.thread_presentations(("beta",)).require("beta")
    assert len(reads) == 1
    assert view.notifications[0].message.reference == initial.message.reference


def test_original_open_certificate_expires_with_canonical_lock(tmp_path):  # noqa: F811
    import sqlite3

    _path, _root_id, comms, initial, _people = _root(tmp_path)
    with comms.bus.log.certified_read() as source:
        assert source.connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert tuple(source.references((initial.message.reference,))) == (initial,)
    assert source.stream.closed
    with pytest.raises(sqlite3.ProgrammingError):
        source.connection.execute("SELECT 1")
    with pytest.raises(RelationViolationError, match="lock lifetime"):
        tuple(source.references((initial.message.reference,)))


def test_missing_certified_wire_cannot_be_an_empty_presentation(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    comms.bus.log.path.unlink()
    with pytest.raises(
        RelationViolationError,
        match=r"^Private bus checkpoint verification is unavailable; durability is UNKNOWN\.$",
    ):
        comms.views.thread_presentations(("beta",)).require("beta")


def test_bus_guard_preserves_the_callers_original_failure(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    failure = OSError("Caller-owned failure after actual durability claim_scope")
    with pytest.raises(OSError) as raised:
        with comms.bus.log.locked():
            raise failure
    assert raised.value is failure
