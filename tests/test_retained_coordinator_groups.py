"""Original harness groups survive selected loading without replaying their lifecycle."""
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agent_comms.assignment_states import FullPendingAssignment
from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.codex import CodexBridge
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.harness_deliveries import HarnessDelivery, HarnessObservedPart
from agent_comms.coordinator import StandaloneCoordination
from agent_comms.harness_delivery_states import EmittedOutcome, ReceivingMonitor
from agent_comms.message_reference import MessageReference
from agent_comms.nominal.field_codec import AssignmentId, MessageId, TranscriptRecordId
from agent_comms.wake_policy import FullWake, MessageAudience
from bus_record_fixture import RECIPIENT_LOOKUP


@dataclass(frozen=True)
class HarnessSource:
    store: StandaloneCoordination
    deliveries: tuple[HarnessDelivery, ...]
    parts: tuple[HarnessObservedPart, ...]
    claims: tuple[WakeAssignment, ...]

    @property
    def path(self) -> Path:
        return self.store.session.path


@pytest.fixture
def harness_source(tmp_path: Path) -> Iterator[HarnessSource]:
    path = tmp_path / 'source' / 'coordination.sqlite3'
    with StandaloneCoordination(str(path), clock_ms=lambda: 1000) as store:
        store.participants.register(RECIPIENT_LOOKUP, committed=True)
        claim = WakeAssignment(
            assignment_id=AssignmentId.cohort(b'original-retained-claim'),
            recipient='Alice', recipient_lookup=RECIPIENT_LOOKUP,
            wire_seq=1, message_id=MessageId.mint('original-first-message'),
            audience=MessageAudience.DIRECT,
            lifecycle=FullPendingAssignment.load(FullWake(), None, None, None),
            accepted_at_ms=1000, updated_at_ms=1000,
        )
        store.assignments.accept(claim)
        monitors = (ReceivingMonitor(2), ReceivingMonitor(1))
        deliveries = (
            HarnessDelivery(ClaudeBridge, RECIPIENT_LOOKUP, claim.message_id, 1,
                            EmittedOutcome, monitors[0]),
            HarnessDelivery(ClaudeBridge, RECIPIENT_LOOKUP,
                            MessageId.mint('original-second-message'), 2,
                            EmittedOutcome, monitors[1]),
        )
        parts = tuple(
            HarnessObservedPart(delivery.bridge, delivery.recipient_lookup, delivery.message_id,
                                number, 'Sender', f'original body {delivery.seq}/{number}',
                                TranscriptRecordId(f'original-record-{delivery.seq}-{number}'),
                                10.0 + delivery.seq + number / 10)
            for delivery, monitor in zip(deliveries, monitors, strict=True)
            for number in range(1, monitor.total + 1)
        )
        with store.session.writing() as db:
            for delivery in deliveries:
                delivery.begin(store.assignments, db)
            for part in parts:
                part.insert(db)
        # Keep the original post-handoff state and revision, not a reconstructed
        # initial claim or a transition performed while materializing the copy.
        yield HarnessSource(store, deliveries, parts,
                            (store.assignments.get(claim.assignment_id),))


@pytest.mark.parametrize('selected_index', (0, 1), ids=('existing-claim', 'no-claim'))
def test_retained_harness_group_keeps_exact_parts_and_existing_claims(
    harness_source: HarnessSource, tmp_path: Path, selected_index: int,
) -> None:
    selected = harness_source.deliveries[selected_index]
    destination = tmp_path / 'retained' / 'coordination.sqlite3'
    before = CoordinationStore.digest(harness_source.path)
    with CoordinationStore.retaining_rows(
        harness_source.path, destination, maximum_bytes=8 * 2**20,
    ) as original:
        original.retain_keys(HarnessDelivery, ({
            'bridge': selected.bridge, 'recipient_lookup': selected.recipient_lookup,
            'message_id': selected.message_id,
        },))
    with StandaloneCoordination(str(destination)) as retained:
        with retained.session.read():
            db = retained.session._connection
            assert tuple(HarnessDelivery.select(db)) == (selected,)
            assert tuple(HarnessObservedPart.select(db, order_by=('number',))) == tuple(
                part for part in harness_source.parts if part.message_id == selected.message_id
            )
            assert tuple(WakeAssignment.select(db)) == tuple(
                claim for claim in harness_source.claims if claim.source == selected.reference
            )
    assert CoordinationStore.digest(harness_source.path) == before


def test_harness_claim_query_refuses_conflicting_full_reference(harness_source: HarnessSource) -> None:
    selected = harness_source.deliveries[0]
    wrong = MessageReference(selected.seq, MessageId.mint('foreign-message-at-same-sequence'))
    with harness_source.store.session.read():
        db = harness_source.store.session._connection
        assert tuple(WakeAssignment.for_harness_sources(
            db, selected.recipient_lookup, (selected.reference, selected.reference),
        )) == harness_source.claims
        assert tuple(WakeAssignment.for_harness_sources(db, selected.recipient_lookup, ())) == ()
        with pytest.raises(IdentityConflict):
            tuple(WakeAssignment.for_harness_sources(db, selected.recipient_lookup, (wrong,)))


def test_retained_harness_conflict_refuses_without_publication(
    harness_source: HarnessSource, tmp_path: Path,
) -> None:
    # A foreign producer can leave a SQL-valid attempt whose natural-key claim
    # names another message. Retention must refuse it, not filter the claim away.
    conflicting = replace(harness_source.deliveries[0], bridge=CodexBridge,
                          message_id=MessageId.mint('foreign-source-message'))
    with harness_source.store.session.writing() as db:
        conflicting.insert(db)
    before = CoordinationStore.digest(harness_source.path)
    destination = tmp_path / 'retained' / 'coordination.sqlite3'
    with pytest.raises(IdentityConflict):
        with CoordinationStore.retaining_rows(
            harness_source.path, destination, maximum_bytes=8 * 2**20,
        ) as original:
            original.retain_keys(HarnessDelivery, ({
                'bridge': conflicting.bridge, 'recipient_lookup': conflicting.recipient_lookup,
                'message_id': conflicting.message_id,
            },))
    assert not destination.exists()
    assert not tuple(destination.parent.iterdir())
    assert CoordinationStore.digest(harness_source.path) == before
