"""Bound real frozen recipient acquisition before assignment/harness/reply decoding.

No model, provider, product monkeypatch or hand-edited coordination row is used.
"""
from __future__ import annotations

from pathlib import Path
import pytest

from agent_comms.bus_publication import Delivered
from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.comms import Comms
from agent_comms.coordination_tables.harness_deliveries import HarnessDelivery
from agent_comms.coordinator import Coordination
from agent_comms.harness_delivery_states import PersistedOutcome
from agent_comms.notification_assignment import NotificationAssignment
from agent_comms.notification_evidence import AnsweredResponse
from agent_comms.read_basis import AllOutcomes, AfterRecipient, OwnerOutcomes
from agent_comms.notification_evidence import RecipientSelection
from agent_comms.presentation import MessageNotification
from agent_comms.read_ledger import ReadLedger
from agent_comms.repo import Checkout
from agent_comms.threads import Thread
from agent_comms.notification_reads import LiveNotificationRead
from agent_comms.ui_model.observation import ReadNotifications
from conftest import accept_initial, publish_initial
from test_coordinated_runtime import _root, tmp_path  # noqa: F401


@pytest.fixture
def frozen_window(tmp_path: Path) -> tuple[Comms, Delivered]:
    root, root_id, comms, _initial, people = _root(tmp_path)
    for index in range(14):
        comms.registry.declare(Thread(
            f"window-{index}", frozenset({"team"}), Checkout(tmp_path),
            process_identity=people[1].process_identity,
        ))
    message = publish_initial(comms, "sender", "#team", "A frozen recipient window")
    source = comms.bus.bus.delivery(message.seq)
    with Coordination(root / "coordination.sqlite3") as store:
        for recipient in source.recipients:
            store.participants.register(recipient.recipient_lookup, committed=True)
        accept_initial(comms.bus, root_id, message.seq, store)
    return comms, source


def record_attempt(comms: Comms, source: Delivered, recipient_position: int) -> None:
    recipient = source.recipients[recipient_position]
    delivery = HarnessDelivery(ClaudeBridge, recipient.recipient_lookup,
                               source.message.message_id, source.message.seq, PersistedOutcome)
    with Coordination(comms.root / "coordination.sqlite3") as store:
        with store.session.writing() as connection:
            assert delivery.begin(store.assignments, connection)


def test_acquired_rows_and_proofs_are_exact_bounded_subset_without_rewriting_original(
    frozen_window: tuple[Comms, Delivered],
) -> None:
    comms, source = frozen_window
    frozen = source.recipients
    before_reads = ReadLedger.at(comms.root).read()
    before_wire = comms.bus.bus.revision()
    viewer = AllOutcomes()
    full = NotificationAssignment.read(comms.root, viewer.selection((source,)))
    selection = viewer.selection((source,), limit=3)
    bounded = NotificationAssignment.read(comms.root, selection)
    selected = {recipient.recipient_lookup for recipient in selection.pages[0].recipients}
    assert len(full.rows) == len(frozen) > len(bounded.rows) == 3
    assert {row.recipient_lookup for row, _activity in bounded.rows} == selected
    assert bounded.sources[0] is source
    assert source.recipients is frozen
    full_window = MessageNotification.delivery_window(comms.root, comms.registry, full)
    bounded_window = MessageNotification.delivery_window(comms.root, comms.registry, bounded)
    key = source.message.seq, source.message.message_id
    assert bounded_window.outcomes[key] == tuple(
        row for row in full_window.outcomes[key] if row.recipient_identity.recipient_lookup in selected)
    assert bounded_window.remaining(source.message.reference) == len(frozen) - 3
    assert ReadLedger.at(comms.root).read() == before_reads
    assert comms.bus.bus.revision() == before_wire


def test_retained_cut_ignores_other_recipient_change_but_observes_its_own_proof(
    frozen_window: tuple[Comms, Delivered],
) -> None:
    comms, source = frozen_window
    selection = AllOutcomes().selection((source,), limit=3)
    before = NotificationAssignment.read(comms.root, selection)
    record_attempt(comms, source, len(source.recipients) - 1)
    assert NotificationAssignment.changed((before,), comms.root) == ()
    record_attempt(comms, source, 0)
    assert NotificationAssignment.changed((before,), comms.root) == (before,)
    changed = NotificationAssignment.read(comms.root, selection)
    assert len(changed.attempts) == 1
    assert changed.attempts[0].recipient_lookup == source.recipients[0].recipient_lookup


def test_cursor_and_owner_scope_are_applied_by_actual_read_request_before_acquisition(
    frozen_window: tuple[Comms, Delivered],
) -> None:
    comms, source = frozen_window
    reference = source.message.reference
    first = ReadNotifications(str(comms.root), LiveNotificationRead((reference,), limit=3)).read(comms)
    key = reference.seq, reference.message_id
    cursor = AfterRecipient(first.outcomes[key][-1].recipient_identity)
    second = ReadNotifications(
        str(comms.root), LiveNotificationRead((reference,), after=cursor, limit=3)).read(comms)
    assert len(first.outcomes[key]) == len(second.outcomes[key]) == 3
    assert {row.recipient_identity for row in first.outcomes[key]}.isdisjoint(
        row.recipient_identity for row in second.outcomes[key])
    assert second.remaining(reference) == len(source.recipients) - 6
    own = source.recipients[-1]
    recipient = comms.views.message_notifications_for_references(
        OwnerOutcomes(own.recipient_lookup), (reference,), limit=1)
    assert tuple(row.recipient_identity for row in recipient.outcomes[key]) == (own,)
    with pytest.raises(ValueError):
        AllOutcomes().selection((source,), limit=RecipientSelection.max_limit + 1)


def test_reply_acquisition_returns_only_first_reply_of_selected_frozen_author(
    frozen_window: tuple[Comms, Delivered],
) -> None:
    comms, source = frozen_window
    selected, other = source.recipients[0], source.recipients[-1]
    first = comms.messaging.send(selected.canonical_thread, source.message.sender, "first answer",
                                 answers=(source.message.reference,)).message
    comms.messaging.send(selected.canonical_thread, source.message.sender, "later answer",
                         answers=(source.message.reference,))
    comms.messaging.send(other.canonical_thread, source.message.sender, "other author's answer",
                         answers=(source.message.reference,))
    replies = comms.bus.bus.recipient_replies(((selected.recipient_lookup, source.message.reference),))
    assert tuple(reply.message.reference for reply in replies) == (first.reference,)


def test_independent_reply_changes_retained_evidence_without_claiming_context(
    frozen_window: tuple[Comms, Delivered],
) -> None:
    comms, source = frozen_window
    selection = AllOutcomes().selection((source,), limit=1)
    recipient = selection.pages[0].recipients[0]
    record_attempt(comms, source, 0)
    before = NotificationAssignment.read(comms.root, selection)
    reads = ReadLedger.at(comms.root).read()
    response = comms.messaging.send(recipient.canonical_thread, source.message.sender,
                                    "An independent answer", answers=(source.message.reference,)).message
    assert NotificationAssignment.changed((before,), comms.root) == (before,)
    after = NotificationAssignment.read(comms.root, selection)
    window = MessageNotification.delivery_window(comms.root, comms.registry, after)
    row, = window.outcomes[source.message.seq, source.message.message_id]
    assert row.answer == response.reference
    assert not row.state.consumed
    assert after.rows == before.rows and after.attempts == before.attempts
    assert ReadLedger.at(comms.root).read() == reads
    later = comms.messaging.send(recipient.canonical_thread, source.message.sender,
                                 "A later independent answer", answers=(source.message.reference,)).message
    assert NotificationAssignment.changed((after,), comms.root) == ()
    assert AnsweredResponse(later.reference, int(later.timestamp * 1000)).answered(row) is row


def test_independent_reply_without_any_claim_is_retained_without_context_or_read_mutation(
    frozen_window: tuple[Comms, Delivered],
) -> None:
    comms, _accepted_source = frozen_window
    message = publish_initial(comms, "sender", "#team", "An original without a handling claim")
    source = comms.bus.bus.delivery(message.seq)
    selection = AllOutcomes().selection((source,), limit=1)
    recipient = selection.pages[0].recipients[0]
    reads = ReadLedger.at(comms.root).read()
    before = NotificationAssignment.read(comms.root, selection)
    assert before.rows == before.attempts == ()
    response = comms.messaging.send(recipient.canonical_thread, source.message.sender,
                                    "An answer without an input claim", answers=(message.reference,)).message
    assert NotificationAssignment.changed((before,), comms.root) == (before,)
    after = NotificationAssignment.read(comms.root, selection)
    assert after.rows == after.attempts == ()
    assert after.responses[recipient.recipient_lookup, message.reference] == AnsweredResponse(
        response.reference, int(response.timestamp * 1000))
    window = MessageNotification.delivery_window(comms.root, comms.registry, after)
    row, = window.outcomes[message.seq, message.message_id]
    assert row.answer == response.reference and row.changed
    assert not row.state.consumed and not row.displayed_to
    assert ReadLedger.at(comms.root).read() == reads
