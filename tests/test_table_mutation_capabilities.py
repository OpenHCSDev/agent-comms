"""Raw SQLite mutations exercise the declaration-derived capability family."""
from contextlib import closing
from dataclasses import dataclass
from typing import Annotated

import pytest

from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.coordination_tables.participants import OwnerGenerations, Participants
from agent_comms.declared_family import DeclaredFamily
from agent_comms.lifecycle import LifecycleState
from agent_comms.typed_table import (
    AccumulatingBits, Column, ColumnGuard, FinalizedLifecycle, FirstValueState,
    FrozenColumns, GuardedColumns, ImmutableRows, IncrementingColumn, LifecycleChanges,
    LifecycleTransitions, MonotoneColumns, NonDecreasing, NonDecreasingWhenPresent,
    NonIncreasing, OneWayColumn, RequiredColumn, TypedTable, WriteOnceColumns,
)
import sqlite3


@dataclass(frozen=True)
class OrderedRow(IncrementingColumn, FrozenColumns, MonotoneColumns, AccumulatingBits, TypedTable):
    identity: Annotated[int, Column(primary_key=True)]
    revision: int
    rising: int
    falling: int
    bits: int
    optional: int | None
    immutable_columns = ("identity",)
    accumulating_columns = ("bits",)
    column_orders = (
        NonDecreasing(("rising",)), NonIncreasing(("falling",)),
        NonDecreasingWhenPresent(("optional",)),
    )


@dataclass(frozen=True)
class SealedRow(OneWayColumn, TypedTable):
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
    with closing(CoordinationStore.writer(":memory:", isolation_level=None)) as connection:
        for row in (OrderedRow, SealedRow, FrozenRow, GuardedRow, PhasedRow, ChangingRow, ReferencedRow):
            row.create(connection)
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


def test_one_way_column_consumes_the_declared_transition(db):
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


def test_owner_counter_refuses_skipping_a_generation():
    with closing(CoordinationStore.writer(":memory:", isolation_level=None)) as connection:
        CoordinatorTable.create_schema(connection)
        Participants(participant_lookup="owner", display_name="owner", committed=True).insert(connection)
        OwnerGenerations(owner_lookup="owner", owner_thread="owner", generation=1).insert(connection)
        with pytest.raises(sqlite3.IntegrityError):
            OwnerGenerations.update(connection, where="owner_lookup='owner'", generation=3)
        OwnerGenerations.update(connection, where="owner_lookup='owner'", generation=2)
