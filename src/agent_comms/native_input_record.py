"""Shared behavior of the two durable native-input declarations.

A row records coordinator ownership, never a process or historical incarnation.
OwnerGenerations is the existing authority for that identity domain.
"""

from abc import ABC

from .coordination_tables.participants import OwnerGenerations


class NativeInputRecord(ABC):
    assignment_id: str
    stage: str
    owner_lookup: str
    owner_thread: str
    owner_generation: int
    execution_id: str | None
    attempt_ordinal: int | None

    @property
    def owner_identity(self) -> OwnerGenerations:
        return OwnerGenerations(
            owner_lookup=self.owner_lookup,
            owner_thread=self.owner_thread,
            generation=self.owner_generation,
        )
