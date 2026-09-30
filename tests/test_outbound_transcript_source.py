"""Whole conversations derive their original frozen wire sources, in both directions."""

from agent_comms import invoke_tool
from agent_comms.comms import wire
from agent_comms.coordination_errors import StaleRevision
from agent_comms.assignment_states import IgnoredAssignment
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.coordinator import Coordination
from agent_comms.notification_assignment import NotificationAssignment
from agent_comms.errors import RelationViolationError
from agent_comms.message_reference import MessageReference
from agent_comms.presentation import MessageNotification
from agent_comms.wire_log import WireLog
from agent_comms.threads import Thread
from agent_comms.transcripts import TranscriptCursor
from test_coordinated_runtime import _root, tmp_path  # noqa: F401

import pytest


def test_publication_intent_joins_original_sender_and_target_only(tmp_path):  # noqa: F811
    from dataclasses import replace
    from agent_comms.field_codec import FieldCodec
    from agent_comms.routing import MessageRoute, TurnRouting
    from agent_comms.transcript_receipts import AssignedTranscriptSource

    _path, _root_id, comms, initial, _people = _root(tmp_path)
    reply = comms.messaging.send_message("beta", "sender", "First original reply")
    source = AssignedTranscriptSource.for_thread(
        comms.root, comms.registry.require("beta"), comms.bus.log
    )
    original = source.rows("w.seq=?", (reply.seq,))
    lookup = stable_thread_lookup(source.recipient.created_at)
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
    receipt = invoke_tool(
        comms,
        "comms_send",
        {
            "from": "beta",
            "to": "sender",
            "body": "Original outgoing ownership reply",
        },
    )
    message = comms.bus.log.message_by_id(receipt["id"])
    after = comms.transcripts.capture_page_read("beta")
    assert after.identity.receipt_frontier.sequence == message.seq
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
    # Renaming cannot reassign the old sender's source to today's spelling.
    comms.registry.rename("beta", "renamed")
    reopened = wire(comms.root).transcripts.capture_page_read("renamed").read()
    assert reopened.events == events
    assert comms.bus.log.full_history()[-1] == message


def test_original_target_handling_revokes_open_sender_read_without_new_message(
    tmp_path,
):  # noqa: F811
    root, _root_id, comms, initial, _people = _root(tmp_path)
    message = initial.message
    before = comms.transcripts.capture_page_read("sender")
    sender_page = before.read()
    target = comms.registry.require("beta")
    lookup = stable_thread_lookup(target.created_at)
    receipts = comms.views.recent_notifications("sender")
    beta = next(item for item in receipts if item.recipient_identity.recipient_lookup == lookup)
    assert beta.message.reference == message.reference and beta.state == "Pending"
    assignments = NotificationAssignment.select(
        root, "w.wire_seq=? AND w.recipient_lookup=?", (message.seq, lookup)
    )
    original = assignments[0].assignment
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
            },
        )
    open_sender = comms.views.thread_presentation("sender")
    open_recipient = comms.views.thread_presentation("beta")
    assert all(item.message.reference != message.reference for item in open_sender.notifications)
    before = comms.transcripts.capture_page_read("sender")
    sender_page = before.read()
    wire_before = comms.bus.log.path.read_bytes()
    with Coordination(str(root / "coordination.sqlite3")) as store:
        store.assignments.transition_preengagement(
            original.assignment_id, IgnoredAssignment, expected_revision=original.revision
        )
    after = comms.transcripts.capture_page_read("sender")
    assert not before.current()
    assert before.content_current()
    assert before.read() == sender_page
    assert after.identity.receipt_frontier == before.identity.receipt_frontier
    assert after.read() == sender_page
    assert comms.bus.log.path.read_bytes() == wire_before
    sender = comms.views.thread_presentation("sender")
    recipient = comms.views.thread_presentation("beta")
    assert sender.notifications == open_sender.notifications
    assert recipient.notifications == open_recipient.notifications
    assert sender.read_identity != open_sender.read_identity
    assert recipient.read_identity != open_recipient.read_identity
    assert sender.read_identity == after.identity
    outcomes = comms.views.message_notifications((message,))[message.seq, message.message_id]
    assert comms.views.message_notifications_for_references((message.reference,)) == {
        (message.seq, message.message_id): outcomes,
    }
    with pytest.raises(RelationViolationError):
        comms.views.message_notifications_for_references(
            (MessageReference(message.seq + 1, message.message_id),)
        )
    assert comms.views.message_notifications_for_references(
        (message.reference,) * (MessageNotification.window_limit + 1)
    ) == {(message.seq, message.message_id): outcomes}
    target_outcome = next(
        item for item in outcomes if item.recipient_identity.recipient_lookup == lookup
    )
    assert target_outcome.state == "Checked — no response"


def test_unrelated_canonical_append_keeps_open_source_content(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    before = comms.transcripts.capture_page_read("beta")
    page = before.read()
    comms.messaging.send_message("sender", "alpha", "Unrelated original")
    after = comms.transcripts.capture_page_read("beta")
    assert before.identity.bus_revision != after.identity.bus_revision
    assert before.identity.receipt_frontier == after.identity.receipt_frontier
    assert not before.current()
    assert before.content_current()
    assert before.read() == page
    assert before.identity.content_identity == after.identity.content_identity
    assert hash(before.identity.content_identity) == hash(after.identity.content_identity)
    comms.messaging.send_message("sender", "beta", "Related original")
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
    verify = WireLog.verify_before_read_unlocked
    barriers = []

    def observed(log):
        if log.path == comms.bus.log.path:
            barriers.append(True)
        return verify(log)

    monkeypatch.setattr(WireLog, "verify_before_read_unlocked", observed)
    view = comms.views.thread_presentation("beta")
    assert len(barriers) == 1
    assert view.read_identity.receipt_frontier.sequence == initial.message.seq
    assert view.notifications[0].message.reference == initial.message.reference
    assert view.notifications[0].recipient_identity.recipient_lookup == stable_thread_lookup(
        view.read_identity.thread.created_at
    )


def test_published_source_binding_keeps_exact_original_request(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    identity = comms.views.thread_presentation("beta").read_identity
    assert comms.transcripts.bind_page_read("beta", identity).identity is identity
    with pytest.raises(StaleRevision, match="original thread"):
        comms.transcripts.bind_page_read("alpha", identity)
    with pytest.raises(StaleRevision, match="another root"):
        wire(tmp_path / "other").transcripts.bind_page_read("beta", identity)
    with pytest.raises(StaleRevision, match="page window"):
        comms.transcripts.bind_page_read(
            "beta", identity,
            before=TranscriptCursor(identity.session_file, 0, identity.receipt_frontier),
        )
    comms.registry.rename("beta", "renamed")
    assert comms.transcripts.bind_page_read("renamed", identity).identity is identity
    comms.registry.unregister("renamed")
    comms.registry.remove("renamed")
    comms.registry.declare(Thread("renamed", frozenset(), str(tmp_path)))
    with pytest.raises(StaleRevision, match="original thread"):
        comms.transcripts.bind_page_read("renamed", identity)


def test_original_page_reuses_captured_frontier_between_admission_fences(tmp_path, monkeypatch):  # noqa: F811
    _path, _root_id, comms, initial, _people = _root(tmp_path)
    read = comms.transcripts.capture_page_read("beta")
    verify = WireLog.verify_before_read_unlocked
    barriers = []

    def observed(log):
        if log.path == comms.bus.log.path:
            barriers.append(True)
        return verify(log)

    monkeypatch.setattr(WireLog, "verify_before_read_unlocked", observed)
    page = read.read()
    # Before admission, one bounded page query, and after admission. The original
    # frontier is already certified: page preparation does not certify it again.
    assert len(barriers) == 3
    assert page.after.receipts == read.identity.receipt_frontier
    assert tuple(event.source for event in page.events) == (initial.message.reference,)


def test_original_page_rejects_relevant_append_during_preparation(tmp_path, monkeypatch):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    read = comms.transcripts.capture_page_read("beta")
    prepare = comms.transcripts.thread_transcript_page

    def append_after_page(*args, **kwargs):
        page = prepare(*args, **kwargs)
        comms.messaging.send_message("sender", "beta", "Original concurrent append")
        return page

    monkeypatch.setattr(comms.transcripts, "thread_transcript_page", append_after_page)
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
    view = comms.views.thread_presentation("beta")
    assert len(reads) == 1
    assert view.notifications[0].message.reference == initial.message.reference


def test_original_open_certificate_expires_with_canonical_lock(tmp_path):  # noqa: F811
    import sqlite3
    from agent_comms.private_bus_checkpoint import source_references_unlocked

    _path, _root_id, comms, initial, _people = _root(tmp_path)
    with comms.bus.log.certified_read() as source:
        assert source.connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert source_references_unlocked(source, (initial.message.reference,)) == (initial.message,)
    assert source.stream.closed
    with pytest.raises(sqlite3.ProgrammingError):
        source.connection.execute("SELECT 1")
    with pytest.raises(RelationViolationError, match="lock lifetime"):
        source_references_unlocked(source, (initial.message.reference,))


def test_missing_certified_wire_cannot_be_an_empty_presentation(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    comms.bus.log.path.unlink()
    with pytest.raises(RelationViolationError, match="inode is missing"):
        comms.views.thread_presentation("beta")


def test_bus_guard_preserves_the_callers_original_failure(tmp_path):  # noqa: F811
    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    failure = OSError("Caller-owned failure after actual durability admission")
    with pytest.raises(OSError) as raised:
        with comms.bus.log.locked():
            raise failure
    assert raised.value is failure
