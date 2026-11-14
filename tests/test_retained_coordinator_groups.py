"""Original harness groups survive selected loading without replaying their lifecycle."""
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, closing
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.codex import CodexBridge
from agent_comms.cli_bridges.harness_delivery_states import EmittedOutcome, ReceivingMonitor
from agent_comms.comms import wire
from agent_comms.coordination.assignment_states import (
    FullPendingAssignment,
    TriagePendingAssignment,
)
from agent_comms.coordination.attempt_start import AttemptStart
from agent_comms.coordination.coordinator import StandaloneCoordination
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.errors import IdentityConflict
from agent_comms.coordination.owner_fence import prepare_fence_token
from agent_comms.coordination.participant_store import ParticipantRows, ParticipantStore
from agent_comms.coordination.reason_codes import GivenReasonCode, NoDetail
from agent_comms.coordination.recovery_states import ModelStalledRecovery
from agent_comms.coordination.tables.assignments import WakeAssignment
from agent_comms.coordination.tables.executions import (
    AcpOrigin,
    CurrentExecutions,
    ExecutionRecord,
)
from agent_comms.coordination.tables.harness_deliveries import HarnessDelivery, HarnessObservedPart
from agent_comms.coordination.tables.participants import OwnerGenerations, Participants
from agent_comms.coordination.tables.recovery import RecoveryAudit
from agent_comms.execution.native_input_record import FullNativeExecution, TriageNativeExecution
from agent_comms.execution.native_runtime_input import NativeRuntimeInput
from agent_comms.execution.triage_native_sources import TriageNativeSource
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import (
    AssignmentId,
    MessageId,
    ThreadLookup,
    TranscriptRecordIdText,
)
from agent_comms.storage.typed_table import (
    AttachedPracticeRebuild,
    NoDelete,
    TypedTable,
    UnallocatedRowId,
)
from agent_comms.waking.wake_policy import FullWake, MessageAudience
from bus_record_fixture import (
    FIRST_INPUT_ID,
    RECIPIENT_LOOKUP,
    SECOND_INPUT_ID,
    THIRD_INPUT_ID,
    prepare_native_comms,
)


def retaining_coordinator(
    source: Path, destination: Path,
) -> AbstractContextManager[AttachedPracticeRebuild]:
    """Use the real coordinator expansion with one shared test-copy allowance."""
    return CoordinationStore.retaining_rows(
        source, destination, maximum_bytes=8 * 2**20, original_type=ParticipantRows,
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
                                TranscriptRecordIdText.decode(str(uuid4())),
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
    with retaining_coordinator(harness_source.path, destination) as original:
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
        with retaining_coordinator(harness_source.path, destination) as original:
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
            owner_token_digest='e' * 64, expected_prompt_digest='d' * 64,
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
    with retaining_coordinator(source.path, destination) as original:
        original.retain_keys(seed, (keys,))
    with closing(CoordinationStore.read_snapshot(destination)) as db:
        assert tuple(NativeRuntimeInput.select(db)) == (selected,)
        assert tuple(WakeAssignment.select(db)) == expected_claims
        assert tuple(TriageNativeSource.select(db)) == tuple(
            TriageNativeSource(selected.input_id, ordinal, claim.assignment_id, 0)
            for ordinal, claim in enumerate(expected_claims)
        )
        assert tuple(row.execution_id for row in ExecutionRecord.select(db)) == execution_ids
        assert ParticipantStore.read_at(db, RECIPIENT_LOOKUP) == source.store.participants.get(RECIPIENT_LOOKUP)
    assert CoordinationStore.digest(source.path) == before


def test_selected_participant_keeps_its_generation_and_current_pointer(
    native_group_source: NativeGroupSource, tmp_path: Path,
) -> None:
    source = native_group_source
    source.store.participants.advance_generation(RECIPIENT_LOOKUP, expected_generation=1)
    unrelated = ThreadLookup('9' * 32)
    absent = ThreadLookup('8' * 32)
    source.store.participants.register(unrelated, committed=True)
    source.store.create('selected-current', AcpOrigin, RECIPIENT_LOOKUP, 1)
    source.store.mark_pending('selected-current', expected_revision=1)
    source.store.start(AttemptStart(
        execution_id='selected-current', attempt_ordinal=1, owner_lookup=RECIPIENT_LOOKUP, owner_generation=2, token=prepare_fence_token(),
        expected_execution_revision=2, expected_pointer_revision=0,
    ))
    expected = source.store.participants.get(RECIPIENT_LOOKUP)
    before = CoordinationStore.digest(source.path)
    destination = tmp_path / 'participant-retained' / 'coordination.sqlite3'
    with retaining_coordinator(source.path, destination) as original:
        with pytest.raises(IdentityConflict):
            ParticipantStore.read_at(original.destination, RECIPIENT_LOOKUP)
        assert ParticipantStore.read_at(
            original.destination, RECIPIENT_LOOKUP, table_source=original.source,
        ) == expected
        ParticipantStore.retain_owners(original, (RECIPIENT_LOOKUP, absent))
    with closing(CoordinationStore.read_snapshot(destination)) as db:
        assert ParticipantStore.read_at(db, RECIPIENT_LOOKUP) == expected
        assert tuple(Participants.select(db)) == (Participants(
            participant_lookup=RECIPIENT_LOOKUP, committed=True,
        ),)
        assert tuple(row.execution_id for row in ExecutionRecord.select(db)) == ('selected-current',)
        assert not NativeRuntimeInput.select(db)
        assert not WakeAssignment.select(db)
    assert expected.owner_identity.generation == 2
    assert expected.pointer.pointer_revision == 1
    assert CoordinationStore.digest(source.path) == before


@pytest.mark.parametrize('missing', (OwnerGenerations, CurrentExecutions),
                         ids=('missing-generation', 'missing-pointer'))
@pytest.mark.parametrize('retain_seed', (
    lambda original, delivery: ParticipantStore.retain_owners(
        original, (delivery.recipient_lookup,),
    ),
    lambda original, delivery: delivery.retain_original(original),
), ids=('direct-owner', 'fk-discovered-owner'))
def test_incomplete_original_participant_refuses_selected_publication(
    native_group_source: NativeGroupSource, tmp_path: Path, missing: type[TypedTable],
    retain_seed: Callable[[AttachedPracticeRebuild, HarnessDelivery], int],
) -> None:
    source = native_group_source
    lookup = ThreadLookup('7' * 32)
    delivery = HarnessDelivery(
        ClaudeBridge, lookup, MessageId.mint('incomplete-owner-message'), 7, EmittedOutcome,
    )
    source.store.participants.register(lookup, committed=True)
    source.store.participants.get(lookup)  # The original aggregate starts valid.
    with source.store.session.writing() as db:
        delivery.insert(db)
        # Explicit foreign corruption, not an admitted coordinator transition:
        # remove only this type's delete guard, leaving insertion membership intact,
        # then restore the exact declared schema before the retained reader sees it.
        delete_guards = NoDelete.mutation_guards(missing)
        for guard in delete_guards:
            db.execute(f'DROP TRIGGER "{guard.name}"')
        missing.delete(db, owner_lookup=lookup)
        for guard in delete_guards:
            db.execute(guard.sql)
        CoordinationStore.schema_at(source.path).require_schema(db)
    with pytest.raises(IdentityConflict):
        source.store.participants.get(lookup)
    before = CoordinationStore.digest(source.path)
    destination = tmp_path / 'incomplete-participant' / 'coordination.sqlite3'
    with pytest.raises(IdentityConflict):
        with retaining_coordinator(source.path, destination) as original:
            retain_seed(original, delivery)
    assert not destination.exists()
    assert not tuple(destination.parent.iterdir())
    assert CoordinationStore.digest(source.path) == before


@pytest.mark.parametrize('keys,selected_count', (
    (({'execution_id': 'sequence-selected'},), 1),
    ((), 0),
), ids=('lower-audit', 'empty-selection'))
def test_selected_store_keeps_sqlite_high_water_without_rowid_mixin(
    harness_source: HarnessSource, tmp_path: Path,
    keys: tuple[dict[str, object], ...], selected_count: int,
) -> None:
    source = harness_source
    other = ThreadLookup('6' * 32)
    source.store.participants.register(other, committed=True)
    for execution, owner in (('sequence-selected', RECIPIENT_LOOKUP), ('sequence-omitted', other)):
        source.store.create(execution, AcpOrigin, owner, 1)
        source.store.mark_pending(execution, expected_revision=1)
        source.store.start(AttemptStart(
            execution_id=execution, attempt_ordinal=1, owner_lookup=owner, owner_generation=1, token=prepare_fence_token(),
            expected_execution_revision=2, expected_pointer_revision=0,
        ))
        # Actual stored audit rows exercise allocation, not model-stall or native proof.
        with source.store.session.writing() as db:
            cursor = RecoveryAudit(
                execution_id=execution, kind=ModelStalledRecovery,
                reason_code=GivenReasonCode('sequence_control'), sanitized_detail=NoDetail(),
                attempt=1, elapsed_ms=0, observed_at_ms=1000,
            ).insert(db)
            source_high_water = cursor.lastrowid
    assert source_high_water is not None
    with source.store.session.read():
        audits = tuple(RecoveryAudit.select(source.store.session._connection, order_by=('audit_id',)))
    before = CoordinationStore.digest(source.path)
    destination = tmp_path / 'sequence-retained' / 'coordination.sqlite3'
    with retaining_coordinator(source.path, destination) as original:
        original.retain_keys(ExecutionRecord, keys)
    with StandaloneCoordination(str(destination), clock_ms=lambda: 1000) as retained:
        with retained.session.read():
            assert tuple(RecoveryAudit.select(retained.session._connection, order_by=('audit_id',))) == audits[:selected_count]
        fresh_owner = ThreadLookup('5' * 32)
        retained.participants.register(fresh_owner, committed=True)
        retained.create('sequence-new', AcpOrigin, fresh_owner, 1)
        retained.mark_pending('sequence-new', expected_revision=1)
        retained.start(AttemptStart(
            execution_id='sequence-new', attempt_ordinal=1, owner_lookup=fresh_owner, owner_generation=1, token=prepare_fence_token(),
            expected_execution_revision=2, expected_pointer_revision=0,
        ))
        with retained.session.writing() as db:
            fresh = replace(audits[0], execution_id='sequence-new', audit_id=UnallocatedRowId())
            assert fresh.insert(db).lastrowid == source_high_water + 1
    assert CoordinationStore.digest(source.path) == before


def test_native_membership_query_uses_the_supplied_attachment(
    native_group_source: NativeGroupSource, tmp_path: Path,
) -> None:
    source = native_group_source
    selected = source.native[0]
    before = CoordinationStore.digest(source.path)
    destination = tmp_path / 'membership-query' / 'coordination.sqlite3'
    with retaining_coordinator(source.path, destination) as original:
        # Both table sets exist. Ordinary lookup sees the empty destination;
        # qualification must keep every join on the original attachment.
        assert selected.execution.source_assignments(original.destination, selected.input_id) == ()
        assert selected.execution.source_assignments(
            original.destination, selected.input_id, table_source=original.source,
        ) == (source.claims[0],)
        assert selected.execution.source_assignment_ids(original.destination, selected.input_id) == ()
        assert selected.execution.source_assignment_ids(
            original.destination, selected.input_id, table_source=original.source,
        ) == (source.claims[0].assignment_id,)
    assert CoordinationStore.digest(source.path) == before
