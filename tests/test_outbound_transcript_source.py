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
from test_coordinated_runtime import _root, tmp_path  # noqa: F401

import pytest


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
