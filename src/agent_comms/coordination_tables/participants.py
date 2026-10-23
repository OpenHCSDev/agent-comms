"""Participant rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_schema import CoordinatorRow
from agent_comms.participant_identity import ParticipantIdentity
from agent_comms.typed_table import (
    Column,
    ColumnGuard,
    CounterAdvance,
    ForeignKey,
    FrozenColumn,
    GuardedColumns,
    ImmutableRows,
    MonotoneColumns,
    NoDelete,
    NonDecreasing,
)


class OwnerRecord:
    """Stored owner components produce one coordinator identity, never live permission."""

    owner_lookup: str
    owner_thread: str
    owner_generation: int

    @property
    def owner_identity(self) -> OwnerGenerations:
        return OwnerGenerations(
            owner_lookup=self.owner_lookup, owner_thread=self.owner_thread,
            generation=self.owner_generation,
        )


@dataclass(frozen=True, kw_only=True)
class Participants(MonotoneColumns, GuardedColumns, NoDelete, CoordinatorRow):

    column_orders = (NonDecreasing(('committed',)),)

    column_guards = (
        ColumnGuard(('participant_lookup',), """1"""),
    )

    participant_lookup: Annotated[
        str,
        Column(
            primary_key=True,
            check="\n        length(participant_lookup) BETWEEN 1 AND 256\n    ",
        ),
    ]
    display_name: Annotated[str, Column(check="length(display_name) BETWEEN 1 AND 256")]
    committed: bool

@dataclass(frozen=True, kw_only=True)
class ParticipantAliases(ImmutableRows, CoordinatorRow):
    alias: Annotated[str, Column(primary_key=True, check="length(alias) BETWEEN 1 AND 256")]
    participant_lookup: str
    renamed_at_ms: Annotated[int, Column(check="renamed_at_ms >= 0")]

    @classmethod
    def references(cls):
        return (ForeignKey(("participant_lookup",), Participants, ("participant_lookup",)),)

@dataclass(frozen=True, kw_only=True)
class OwnerGenerations(
    CounterAdvance, NoDelete, ParticipantIdentity, CoordinatorRow
):

    counter_column = 'generation'

    owner_lookup: Annotated[str, FrozenColumn(primary_key=True)]
    owner_thread: Annotated[str, Column(check="length(owner_thread) BETWEEN 1 AND 256")]
    generation: Annotated[int, Column(check="generation > 0")]

    def require_recorded_owner(self, thread):
        """Join original coordinator naming/birth; grant no live ownership."""
        expected = type(self)(owner_lookup=thread.incarnation.lookup,
                              owner_thread=thread.name, generation=self.generation)
        if self != expected:
            raise IdentityConflict("Native input belongs to another recorded owner")

    @classmethod
    def references(cls):
        return (ForeignKey(("owner_lookup",), Participants, ("participant_lookup",)),)

    @property
    def lookup(self) -> str:
        return self.owner_lookup
