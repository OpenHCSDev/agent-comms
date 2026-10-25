"""Participant rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_schema import CoordinatorRow
from agent_comms.field_codec import ThreadLookup
from agent_comms.participant_identity import ParticipantIdentity
from agent_comms.typed_table import (
    Column,
    ColumnGuard,
    CounterAdvance,
    ForeignKey,
    FrozenColumn,
    GuardedColumns,
    MonotoneColumns,
    NoDelete,
    NonDecreasing,
)


class OwnerRecord:
    """Stored owner components produce one coordinator identity, never live permission."""

    owner_lookup: ThreadLookup
    owner_generation: int

    @property
    def owner_identity(self) -> OwnerGenerations:
        return OwnerGenerations(
            owner_lookup=self.owner_lookup,
            generation=self.owner_generation,
        )


@dataclass(frozen=True, kw_only=True)
class Participants(MonotoneColumns, GuardedColumns, NoDelete, CoordinatorRow):

    column_orders = (NonDecreasing(('committed',)),)

    column_guards = (
        ColumnGuard(('participant_lookup',), """1"""),
    )

    participant_lookup: Annotated[
        ThreadLookup,
        Column(
            primary_key=True,
            check="\n        length(participant_lookup) BETWEEN 1 AND 256\n    ",
        ),
    ]
    committed: bool

@dataclass(frozen=True, kw_only=True)
class OwnerGenerations(
    CounterAdvance, NoDelete, ParticipantIdentity, CoordinatorRow
):

    counter_column = 'generation'

    owner_lookup: Annotated[ThreadLookup, FrozenColumn(primary_key=True)]
    generation: Annotated[int, Column(check="generation > 0")]

    def within(self, current: OwnerGenerations) -> bool:
        """The same participant at this generation or an earlier, retried one."""
        return self.owner_lookup == current.owner_lookup and self.generation <= current.generation

    def require_recorded_owner(self, thread):
        """Join the recorded incarnation; a rename does not change ownership."""
        expected = type(self)(owner_lookup=thread.incarnation.lookup,
                              generation=self.generation)
        if self != expected:
            raise IdentityConflict("Native input belongs to another recorded owner")

    @classmethod
    def references(cls):
        return (ForeignKey(columns=("owner_lookup",), target=Participants, target_columns=("participant_lookup",)),)

    @property
    def lookup(self) -> ThreadLookup:
        return self.owner_lookup
