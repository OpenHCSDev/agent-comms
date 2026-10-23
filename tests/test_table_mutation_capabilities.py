"""Raw SQLite mutations exercise the declaration-derived capability family."""
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Annotated, ClassVar

import pytest

from conftest import maintenance_held

from agent_comms.coordination_database import CoordinationStore
from agent_comms.locked_store import SQLiteFile
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.coordination_tables.executions import ExecutionRecord
from agent_comms.coordination_tables.participants import OwnerGenerations, Participants
from agent_comms.declared_family import DeclaredFamily
from agent_comms.execution_states import (
    ActiveExecution,
    CompletedExecution,
    DeferredExecution,
    ExecutionState,
    PendingExecution,
)
from agent_comms.lifecycle import LifecycleState
from agent_comms.typed_table import (
    AccumulatingBits,
    Column,
    ColumnGuard,
    FinalizedColumn,
    FinalizedLifecycle,
    FirstValueState,
    FrozenColumn,
    Generated,
    GuardedAfterWrites,
    GuardedColumns,
    GuardedInserts,
    GuardedRelatedInserts,
    GuardedUpdateMembership,
    ImmutableRows,
    IncrementingColumn,
    InitialOneWayColumn,
    LifecycleChanges,
    LifecycleTransitions,
    MonotoneColumns,
    NonDecreasing,
    NonDecreasingWhenPresent,
    NonIncreasing,
    OrdinalTransitions,
    RequiredColumn,
    RequiredUpdateColumns,
    RowMutation,
    StableReferenceColumns,
    TypedTable,
    ValidatedInserts,
    WriteOnceColumns,
)


@dataclass(frozen=True)
class OrderedRow(IncrementingColumn, MonotoneColumns, AccumulatingBits, TypedTable):
    identity: Annotated[int, FrozenColumn(primary_key=True)]
    revision: int
    rising: int
    falling: int
    bits: int
    optional: int | None
    accumulating_columns = ("bits",)
    column_orders = (
        NonDecreasing(("rising",)), NonIncreasing(("falling",)),
        NonDecreasingWhenPresent(("optional",)),
    )


@dataclass(frozen=True)
class ComputedRow(OrderedRow):
    offset: int = 0
    parity: ClassVar[Generated] = Generated(
        int, FrozenColumn(generated="(rising + offset) % 2")
    )


@dataclass(frozen=True)
class SealedRow(InitialOneWayColumn, TypedTable):
    sealed: int
    transition_column = "sealed"
    transition_from = 0
    transition_to = 1


@dataclass(frozen=True)
class FrozenRow(ImmutableRows, TypedTable):
    value: int


@dataclass(frozen=True)
class GuardedRow(GuardedColumns, TypedTable):
    value: int
    column_guards = (ColumnGuard(("value",)),)


@dataclass(frozen=True)
class InsertCheckedRow(GuardedInserts, GuardedUpdateMembership, TypedTable):
    value: int
    insert_condition = "NEW.value < 0"
    update_membership_condition = "NEW.value < 0"


@dataclass(frozen=True)
class RelatedGuardRow(GuardedRelatedInserts, TypedTable):
    value: int
    related_insert_target = InsertCheckedRow
    related_insert_condition = "NEW.value > 10"


@dataclass(frozen=True)
class AfterWriteRow(GuardedAfterWrites, TypedTable):
    value: int
    after_write_condition = "NEW.value < 0"


@dataclass(frozen=True)
class ValidatedRow(ValidatedInserts, TypedTable):
    value: int
    validator_function = "test_positive"


@dataclass(frozen=True)
class ProofRow(WriteOnceColumns, RequiredUpdateColumns, FinalizedColumn, TypedTable):
    claim: int | None
    session: str | None
    proof: int | None
    write_once_columns = ("claim", "session")
    required_update_columns = ("claim",)
    finalized_column = "proof"


@dataclass(frozen=True)
class PointerRow(StableReferenceColumns, TypedTable):
    ordinal: int
    value: int | None
    reference_column = "ordinal"
    stable_reference_columns = ("value",)


class Phase(DeclaredFamily, LifecycleState, affix="Phase"):
    pass


@dataclass(frozen=True)
class StartPhase(Phase):
    @classmethod
    def successors(cls):
        return (EndPhase,)


@dataclass(frozen=True)
class EndPhase(Phase):
    @classmethod
    def successors(cls):
        return ()


@dataclass(frozen=True)
class PhasedRow(LifecycleTransitions, FinalizedLifecycle, TypedTable):
    identity: Annotated[int, Column(primary_key=True)]
    lifecycle: Phase


@dataclass(frozen=True)
class ChangingRow(LifecycleChanges, PhasedRow):
    pass


@dataclass(frozen=True)
class OrdinalRow(LifecycleTransitions, OrdinalTransitions, TypedTable):
    lifecycle: ExecutionState
    current_attempt_ordinal: ClassVar[Generated] = vars(ExecutionRecord)["current_attempt_ordinal"]
    max_attempts: int = 3


@dataclass(frozen=True)
class ReferencedRow(WriteOnceColumns, FirstValueState, RequiredColumn, TypedTable):
    lifecycle: Phase
    value: int | None
    reference: int | None
    write_once_columns = ("value",)
    initial_value_column = "value"
    initial_value_state = StartPhase
    dependent_column = "value"
    required_column = "reference"


@pytest.fixture
def db():
    with closing(SQLiteFile.writer(":memory:", isolation_level=None)) as connection:
        for row in (OrderedRow, ComputedRow, SealedRow, FrozenRow, GuardedRow, PhasedRow, ChangingRow,
                    ReferencedRow, InsertCheckedRow, RelatedGuardRow, AfterWriteRow, ValidatedRow,
                    OrdinalRow, ProofRow, PointerRow):
            row.create(connection)
        connection.create_function("test_positive", 1, lambda value: int(value > 0))
        yield connection


@pytest.mark.parametrize("changes", [
    {"revision": 3}, {"identity": 2}, {"rising": 9}, {"falling": 11},
    {"bits": 0}, {"optional": 9},
])
def test_ordered_columns_refuse_regression(db, changes):
    OrderedRow(1, 1, 10, 10, 1, 10).insert(db)
    with pytest.raises(sqlite3.IntegrityError):
        OrderedRow.update(db, where="identity=1", **({"revision": 2} | changes))
    assert OrderedRow.select(db) == [OrderedRow(1, 1, 10, 10, 1, 10)]


def test_ordered_columns_accept_progress_and_optional_retirement(db):
    OrderedRow(1, 1, 10, 10, 1, 10).insert(db)
    OrderedRow.update(db, where="identity=1", revision=2, rising=11, falling=9, bits=3, optional=None)
    assert OrderedRow.select(db) == [OrderedRow(1, 2, 11, 9, 3, None)]


def test_frozen_fields_include_inherited_and_generated_columns(db):
    original = ComputedRow(1, 1, 10, 10, 1, 10)
    original.insert(db)
    for changes in ({"identity": 2}, {"rising": 11}, {"offset": 1}):
        with pytest.raises(sqlite3.IntegrityError):
            ComputedRow.update(db, where="identity=1", **({"revision": 2} | changes))
    ComputedRow.update(db, where="identity=1", revision=2, rising=12, offset=2)
    assert ComputedRow.select(db) == [ComputedRow(1, 2, 12, 10, 1, 10, 2)]


def test_one_way_column_consumes_the_declared_transition(db):
    with pytest.raises(sqlite3.IntegrityError):
        SealedRow(1).insert(db)
    SealedRow(0).insert(db)
    SealedRow.update(db, where="1", sealed=1)
    for value in (0, 1):
        with pytest.raises(sqlite3.IntegrityError):
            SealedRow.update(db, where="1", sealed=value)


@pytest.mark.parametrize("statement", ["UPDATE frozen SET value=2", "DELETE FROM frozen"])
def test_immutable_rows_refuse_both_destructive_events(db, statement):
    FrozenRow(1).insert(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(statement)


def test_column_selection_is_guarded_even_when_the_value_is_equal(db):
    GuardedRow(1).insert(db)
    with pytest.raises(sqlite3.IntegrityError):
        GuardedRow.update(db, where="1", value=1)


def test_lifecycle_edges_and_terminal_payload_are_owned(db):
    PhasedRow(1, StartPhase()).insert(db)
    PhasedRow.update(db, where="identity=1", lifecycle=EndPhase())
    for phase in (StartPhase(), EndPhase()):
        with pytest.raises(sqlite3.IntegrityError):
            PhasedRow.update(db, where="identity=1", lifecycle=phase)
    ChangingRow(1, StartPhase()).insert(db)
    with pytest.raises(sqlite3.IntegrityError):
        ChangingRow.update(db, where="identity=1", lifecycle=StartPhase())


def test_nullable_reference_is_written_once_in_its_declared_state(db):
    ReferencedRow(StartPhase(), None, None).insert(db)
    with pytest.raises(sqlite3.IntegrityError):
        ReferencedRow.update(db, where="1", value=1)
    ReferencedRow.update(db, where="1", value=1, reference=2)
    with pytest.raises(sqlite3.IntegrityError):
        ReferencedRow.update(db, where="1", value=2)
    db.execute("DELETE FROM referenced")
    ReferencedRow(EndPhase(), None, 2).insert(db)
    with pytest.raises(sqlite3.IntegrityError):
        ReferencedRow.update(db, where="1", value=1)


@pytest.mark.parametrize("previous, following, allowed", [
    (PendingExecution(), ActiveExecution(1), True),
    (PendingExecution(), ActiveExecution(2), False),
    (ActiveExecution(1), CompletedExecution(2), False),
    (DeferredExecution(1), ActiveExecution(2), True),
])
def test_python_and_sql_use_the_state_ordinal_declaration(db, previous, following, allowed):
    assert following.accepts_previous(previous) is allowed
    OrdinalRow(previous).insert(db)
    if allowed:
        OrdinalRow.update(db, where="1", lifecycle=following)
    else:
        with pytest.raises(sqlite3.IntegrityError):
            OrdinalRow.update(db, where="1", lifecycle=following)


@pytest.mark.parametrize("value", [-1, 11])
def test_insert_and_related_membership_are_composed(db, value):
    with pytest.raises(sqlite3.IntegrityError):
        InsertCheckedRow(value).insert(db)
    InsertCheckedRow(1).insert(db)


def test_update_membership_is_guarded(db):
    InsertCheckedRow(1).insert(db)
    with pytest.raises(sqlite3.IntegrityError):
        InsertCheckedRow.update(db, where="1", value=-1)


def test_proof_requires_its_reference_and_then_freezes_the_row(db):
    ProofRow(None, None, None).insert(db)
    with pytest.raises(sqlite3.IntegrityError):
        ProofRow.update(db, where="1", session="session")
    ProofRow.update(db, where="1", claim=1)
    ProofRow.update(db, where="1", session="session")
    with pytest.raises(sqlite3.IntegrityError):
        ProofRow.update(db, where="1", session="different")
    ProofRow.update(db, where="1", proof=1)
    with pytest.raises(sqlite3.IntegrityError):
        ProofRow.update(db, where="1", claim=1)


def test_pointer_values_require_a_new_reference(db):
    PointerRow(0, None).insert(db)
    with pytest.raises(sqlite3.IntegrityError):
        PointerRow.update(db, where="1", value=1)
    PointerRow.update(db, where="1", ordinal=1, value=1)
    assert PointerRow.select(db) == [PointerRow(1, 1)]


def test_after_write_guard_refuses_both_events(db):
    with pytest.raises(sqlite3.IntegrityError):
        AfterWriteRow(-1).insert(db)
    AfterWriteRow(1).insert(db)
    with pytest.raises(sqlite3.IntegrityError):
        AfterWriteRow.update(db, where="1", value=-1)


def test_declared_insert_validator_receives_the_field_values(db):
    with pytest.raises(sqlite3.IntegrityError):
        ValidatedRow(-1).insert(db)
    ValidatedRow(1).insert(db)
    assert ValidatedRow.select(db) == [ValidatedRow(1)]


def test_owner_counter_refuses_skipping_a_generation():
    with closing(SQLiteFile.writer(":memory:", isolation_level=None)) as connection:
        CoordinatorTable.create_schema(connection)
        Participants(participant_lookup="owner", committed=True).insert(connection)
        OwnerGenerations(owner_lookup="owner",  generation=1).insert(connection)
        with pytest.raises(sqlite3.IntegrityError):
            OwnerGenerations.update(connection, where="owner_lookup='owner'", generation=3)
        OwnerGenerations.update(connection, where="owner_lookup='owner'", generation=2)


def test_normal_writer_replaces_old_guards_without_rewriting_rows(tmp_path):
    path = tmp_path / CoordinationStore.filename
    with closing(CoordinationStore(path)) as store:
        connection = store._connection
        Participants(participant_lookup="owner", committed=True).insert(
            connection
        )
        before = Participants.select(connection)
        delete_guard = RowMutation.DELETE.abort_trigger(Participants).name
        connection.execute(f'DROP TRIGGER "{delete_guard}"')
        connection.execute(
            "CREATE TRIGGER previous_guard BEFORE DELETE ON participants "
            "BEGIN SELECT RAISE(ABORT, 'frozen'); END"
        )
        assert CoordinatorTable.schema_differs(connection)
    with maintenance_held(tmp_path), closing(CoordinationStore(path)) as store:
        connection = store._connection
        CoordinatorTable.require_installed(connection)
        assert Participants.select(connection) == before
        assert connection.total_changes == 0
        version = connection.execute("PRAGMA schema_version").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            Participants.delete(connection)
    with closing(CoordinationStore(path)) as store:
        connection = store._connection
        assert connection.execute("PRAGMA schema_version").fetchone()[0] == version
        assert connection.total_changes == 0
        assert Participants.select(connection) == before
