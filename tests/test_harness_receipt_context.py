"""Interpret actual persisted receipt variants without starting a harness/model."""
from dataclasses import replace

import pytest

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.coordination_tables.harness_deliveries import HarnessDelivery
from agent_comms.coordinator import Coordination
from agent_comms.harness_delivery_states import (
    AcceptedOutcome, DeliveredOutcome, EmittedOutcome, FilteredOutcome,
    ObservedMonitor, PersistedOutcome, UnconfirmedOutcome, UnrecordedMonitor,
)
from agent_comms.notification_progress import HarnessConsumedProgress
from agent_comms.notification_evidence import UnansweredResponse
from agent_comms.notification_evidence import AllOutcomes
from test_coordinated_runtime import _root, tmp_path  # noqa: F401
from transcript_record_fixture import transcript_record


@pytest.mark.parametrize(('outcome', 'monitor', 'consumed'), (
    (UnconfirmedOutcome, UnrecordedMonitor(), False),
    (EmittedOutcome, UnrecordedMonitor(), False),
    (FilteredOutcome, UnrecordedMonitor(), False),
    (DeliveredOutcome, UnrecordedMonitor(), False),
    (AcceptedOutcome, UnrecordedMonitor(), False),
    (PersistedOutcome, UnrecordedMonitor(), False),
    (DeliveredOutcome, ObservedMonitor(transcript_record('complete-original'), 1791964800.125), True),
))
def test_only_retained_complete_body_proof_promotes_consumption(
    tmp_path, outcome, monitor, consumed,
):
    """This exercises projection of recorded evidence, not transcript acquisition.

    Complete body/sender/part acquisition is protected by test_monitor_delivery;
    an unproved old Delivered row deliberately stays unproved here.
    """
    _path, _root_id, comms, initial, _people = _root(tmp_path, direct=True)
    source = initial.message
    recipient, = comms.bus.bus.delivery(source.seq).recipients
    attempt = HarnessDelivery(ClaudeBridge, recipient.recipient_lookup,
                              source.message_id, source.seq, UnconfirmedOutcome)
    with Coordination(comms.root / 'coordination.sqlite3') as store:
        with store.session.writing() as db:
            assert attempt.begin(store.assignments, db)
            recorded = attempt.apply(
                lambda row: replace(row, outcome=outcome, monitor=monitor),
                store.assignments, db,
            )
        with store.session.writing() as db:
            assert not attempt.begin(store.assignments, db)
            retained, = recorded.stored(db)
            assert retained == recorded
    current, = comms.views.message_notifications_for_references(
        AllOutcomes(), (source.reference,),
    ).outcomes[source.seq, source.message_id]
    assert current.state.consumed is consumed
    assert current.answer == UnansweredResponse()
    assert not current.displayed_to
    assert recorded.monitor == monitor
    if consumed:
        assert current.label == current.timed_state(HarnessConsumedProgress.label, 1791964800125)
