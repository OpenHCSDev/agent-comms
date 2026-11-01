"""Original harness groups survive selected loading without replaying their lifecycle."""
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agent_comms.assignment_states import FullPendingAssignment, TriagePendingAssignment
from agent_comms.comms import wire
from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.codex import CodexBridge
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.harness_deliveries import HarnessDelivery, HarnessObservedPart
from agent_comms.coordination_tables.executions import AcpOrigin, ExecutionRecord
from agent_comms.coordinator import StandaloneCoordination
from agent_comms.harness_delivery_states import EmittedOutcome, ReceivingMonitor
from agent_comms.message_reference import MessageReference
from agent_comms.native_input_record import FullNativeExecution, TriageNativeExecution
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.nominal.field_codec import AssignmentId, MessageId, TranscriptRecordId
from agent_comms.triage_native_sources import TriageNativeSource
from agent_comms.typed_table import TypedTable
from agent_comms.wake_policy import FullWake, MessageAudience
from bus_record_fixture import (
    FIRST_INPUT_ID, SECOND_INPUT_ID, THIRD_INPUT_ID, RECIPIENT_LOOKUP, prepare_native_comms,
)


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


@dataclass(frozen=True)
class NativeGroupSource:
    store: StandaloneCoordination
    native: tuple[NativeRuntimeInput, ...]
    claims: tuple[WakeAssignment, ...]

    @property
    def path(self) -> Path:
        return self.store.session.path


@pytest.fixture
def native_group_source(tmp_path: Path) -> Iterator[NativeGroupSource]:
    comms = prepare_native_comms(wire(tmp_path / 'native-source'))
    with StandaloneCoordination(str(comms.root / 'coordination.sqlite3'),
                                clock_ms=lambda: 1000) as store:
        store.participants.register(RECIPIENT_LOOKUP, committed=True)
        claims = tuple(WakeAssignment(
            assignment_id=AssignmentId.cohort(f'original-triage-{number}'.encode()),
            recipient='Alice', recipient_lookup=RECIPIENT_LOOKUP, wire_seq=number,
            message_id=MessageId.mint(f'original-native-message-{number}'),
            audience=MessageAudience.DIRECT, lifecycle=TriagePendingAssignment(),
            accepted_at_ms=1000, updated_at_ms=1000,
        ) for number in (1, 2))
        for claim in claims:
            store.assignments.accept(claim)
        store.create('original-claimless-full', AcpOrigin, RECIPIENT_LOOKUP, 1)
        stages = (TriageNativeExecution(), TriageNativeExecution(),
                  FullNativeExecution('original-claimless-full', 1))
        native = tuple(NativeRuntimeInput(
            input_id=input_id, stage=type(stage), **stage.binding_fields(),
            owner_lookup=RECIPIENT_LOOKUP, owner_generation=1,
            owner_token_digest='e' * 64,
        ) for input_id, stage in zip(
            (FIRST_INPUT_ID, SECOND_INPUT_ID, THIRD_INPUT_ID), stages, strict=True,
        ))
        with store.session.writing() as db:
            for row, members in zip(native, ((claims[0],), (claims[1],), ()), strict=True):
                row.insert(db)
                row.execution.record_sources(db, row.input_id, members)
        # These are original reserved SQL groups, not actual native admission or
        # a running practice root. No provider or native process is started.
        yield NativeGroupSource(store, native, claims)


@pytest.mark.parametrize('seed,keys,selected_index,claim_count,execution_ids', (
    (NativeRuntimeInput, {'input_id': FIRST_INPUT_ID}, 0, 1, ()),
    (ExecutionRecord, {'execution_id': 'original-claimless-full'}, 2, 0, ('original-claimless-full',)),
), ids=('triage-input', 'full-execution'))
def test_retained_native_group_uses_original_membership_and_children(
    native_group_source: NativeGroupSource, tmp_path: Path, seed: type[TypedTable],
    keys: dict[str, object], selected_index: int, claim_count: int, execution_ids: tuple[str, ...],
) -> None:
    source = native_group_source
    selected = source.native[selected_index]
    expected_claims = source.claims[:claim_count]
    before = CoordinationStore.digest(source.path)
    destination = tmp_path / 'native-retained' / 'coordination.sqlite3'
    with CoordinationStore.retaining_rows(source.path, destination, maximum_bytes=8 * 2**20) as original:
        original.retain_keys(seed, (keys,))
    with closing(CoordinationStore.read_snapshot(destination)) as db:
        assert tuple(NativeRuntimeInput.select(db)) == (selected,)
        assert tuple(WakeAssignment.select(db)) == expected_claims
        assert tuple(TriageNativeSource.select(db)) == tuple(
            TriageNativeSource(selected.input_id, ordinal, claim.assignment_id)
            for ordinal, claim in enumerate(expected_claims)
        )
        assert tuple(row.execution_id for row in ExecutionRecord.select(db)) == execution_ids
    assert CoordinationStore.digest(source.path) == before


def test_native_membership_query_uses_the_supplied_attachment(
    native_group_source: NativeGroupSource, tmp_path: Path,
) -> None:
    source = native_group_source
    selected = source.native[0]
    before = CoordinationStore.digest(source.path)
    destination = tmp_path / 'membership-query' / 'coordination.sqlite3'
    with CoordinationStore.retaining_rows(source.path, destination, maximum_bytes=8 * 2**20) as original:
        # Both table sets exist. Ordinary lookup sees the empty destination;
        # qualification must keep every join on the original attachment.
        assert selected.execution.source_assignment_ids(original.destination, selected.input_id) == ()
        assert selected.execution.source_assignment_ids(
            original.destination, selected.input_id, table_source=original.source,
        ) == (source.claims[0].assignment_id,)
    assert CoordinationStore.digest(source.path) == before
