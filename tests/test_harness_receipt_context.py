"""Interpret actual persisted receipt variants without starting a harness/model."""
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace

import pytest

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.bus import Bus
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_tables.harness_deliveries import HarnessDelivery
from agent_comms.input_attempt import ReservedInput
from agent_comms.input_disposition import InputDispositions
from agent_comms.installer.practice import PracticeCopy
from agent_comms.nominal.field_codec import InputId, TurnId
from agent_comms.coordinator import Coordination
from agent_comms.harness_delivery_states import (
    AcceptedOutcome, DeliveredOutcome, EmittedOutcome, FilteredOutcome,
    ObservedMonitor, PersistedOutcome, UnconfirmedOutcome, UnrecordedMonitor,
)
from agent_comms.notification_progress import HarnessConsumedProgress
from agent_comms.notification_progress import UnansweredResponse
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


def _record_attempt(ledger: InputDispositions, attempt: ReservedInput) -> None:
    ledger.record_originals(attempt)


def _start_attempt(ledger: InputDispositions, attempt: ReservedInput) -> None:
    _record_attempt(ledger, attempt)
    turn, native = TurnId.mint(), InputId.mint()
    assert ledger.bind(attempt.key, claim_scope=attempt.claim_generation,
                       turn_id=turn, native_id=native, text=attempt.source_text)
    assert ledger.started(attempt.key, turn_id=turn, native_id=native, text=attempt.source_text)


@pytest.mark.parametrize(("record", "retained_attempts"), (
    (lambda ledger, attempt: None, 1),
    (_record_attempt, 1),
    (_start_attempt, 0),
))
def test_selected_coordinator_materializer_uses_the_original_unresolved_projection(
    tmp_path, record: Callable[[InputDispositions, ReservedInput], None], retained_attempts: int,
):
    """Original absence stays visible; a stored completed input cannot resurrect."""
    path, _root_id, comms, initial, people = _root(tmp_path, direct=True)
    recipient, = initial.recipients
    original = HarnessDelivery(ClaudeBridge, recipient.recipient_lookup,
                               initial.message.message_id, initial.message.seq, EmittedOutcome)
    with Coordination(CoordinationStore.path_in(path)) as store:
        with store.session.writing() as db:
            assert original.begin(store.assignments, db)
    entry = comms.registry.store.snapshot_with_authority().snapshot.entry(people[2].name)
    ledger = InputDispositions(path)
    attempt, = ledger.harness_attempts((entry,))
    record(ledger, attempt)
    expected = ledger.unresolved((entry,))
    coordinator_before = CoordinationStore.path_in(path).read_bytes()
    bus_before = comms.bus.bus.revision
    copy = PracticeCopy(path, tmp_path / "selected-inputs")
    copy.include((Bus.path_in(path), ledger.path))
    selected = copy.include_inputs((entry,), maximum_bytes=3 * 2**30)
    assert selected == expected
    assert InputDispositions(copy.path).unresolved((entry,)) == expected
    with closing(CoordinationStore.read_snapshot(CoordinationStore.path_in(copy.path))) as db:
        retained = tuple(HarnessDelivery.select(db))
    assert len(retained) == retained_attempts
    assert all(row == original for row in retained)
    assert CoordinationStore.path_in(path).read_bytes() == coordinator_before
    assert comms.bus.bus.revision == bus_before
