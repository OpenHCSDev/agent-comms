"""Participant store: owned coordinator state and transitions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from agent_comms.coordination.errors import (
    IdentityConflict,
    StaleRevision,
)
from agent_comms.coordination.results import AlreadyApplied, Applied
from agent_comms.coordination.session import CoordinationSession
from agent_comms.coordination.tables.executions import (
    CurrentExecutions,
    NoPointedAttempt,
    NoPointedExecution,
)
from agent_comms.coordination.tables.participants import OwnerGenerations, Participants
from agent_comms.nominal.field_codec import ThreadLookup
from agent_comms.storage.typed_table import (
    LOCAL_TABLE_SOURCE,
    AttachedPracticeRebuild,
    RowNotFoundError,
    TableSchema,
    TableSource,
)


@dataclass(frozen=True, slots=True)
class ParticipantSnapshot:
    """Stored coordinator assignment, independent of the process's input permission.

    This generation begins at registration and advances on explicit coordinator
    reassignment/retry, even if the registry process has not changed. It must
    never be substituted for the registry's owner or input generation.
    """

    lookup: ThreadLookup
    committed: bool
    owner_identity: OwnerGenerations
    pointer: CurrentExecutions

    def retain_original(self, original: AttachedPracticeRebuild) -> int:
        """The generation and pointer are both required parts of this recorded answer."""
        return (self.owner_identity.retain_original(original)
                + self.pointer.retain_original(original))


class ParticipantStore:
    def __init__(self, session: CoordinationSession) -> None:
        self.session = session

    def get(self, lookup: ThreadLookup) -> ParticipantSnapshot:
        with self.session.read():
            return self.read_at(self.session._connection, lookup)

    @staticmethod
    def read_at(
        db: sqlite3.Connection, lookup: ThreadLookup, *,
        table_source: TableSource = LOCAL_TABLE_SOURCE,
    ) -> ParticipantSnapshot:
        """Read the same aggregate through the caller's already-owned SQL lifetime."""
        try:
            person = Participants.require(db, participant_lookup=lookup, table_source=table_source)
            generation = OwnerGenerations.require(
                db, owner_lookup=lookup, table_source=table_source,
            )
            pointer = CurrentExecutions.require(db, owner_lookup=lookup, table_source=table_source)
        except RowNotFoundError as error:
            raise IdentityConflict(
                f"participant {lookup!r} aggregate is not registered",
            ) from error
        return ParticipantSnapshot(lookup, person.committed, generation, pointer)

    @classmethod
    def retain_owners(
        cls, original: AttachedPracticeRebuild, lookups: tuple[ThreadLookup, ...],
    ) -> int:
        """Keep selected original aggregates, not every participant's execution history.

        An absent participant remains absent. A present but incomplete aggregate
        follows the ordinary reader's refusal before any file can be published.
        """
        changed = 0
        for person in Participants.select_keys(
            original.destination, tuple({"participant_lookup": lookup} for lookup in lookups),
            table_source=original.source,
        ):
            snapshot = cls.read_at(original.destination, person.participant_lookup,
                                   table_source=original.source)
            changed += person.retain_original(original)
            changed += snapshot.retain_original(original)
        return changed

    def register(
        self,
        lookup: ThreadLookup,
        *,
        committed: bool = False,
    ) -> Applied[ParticipantSnapshot] | AlreadyApplied[ParticipantSnapshot]:
        with self.session.writing() as db:
            existing = Participants.one(self.session._connection, participant_lookup=lookup)
            if existing is not None:
                snapshot = self.get(lookup)
                return AlreadyApplied(snapshot)
            Participants(
                participant_lookup=lookup, committed=committed
            ).insert(db)
            OwnerGenerations(owner_lookup=lookup, generation=1).insert(
                db
            )
            CurrentExecutions(
                owner_lookup=lookup, execution_id=NoPointedExecution(), attempt_ordinal=NoPointedAttempt(),
                pointer_revision=0,
            ).insert(db)
            return Applied(self.get(lookup))

    def commit(
        self, lookup: ThreadLookup
    ) -> Applied[ParticipantSnapshot] | AlreadyApplied[ParticipantSnapshot]:
        with self.session.writing() as db:
            person = self.get(lookup)
            if person.committed:
                return AlreadyApplied(person)
            Participants.update(
                db,
                where="participant_lookup=? AND committed=0",
                parameters=(lookup,),
                committed=True,
            )
            return Applied(self.get(lookup))

    def advance_generation(
        self,
        lookup: ThreadLookup,
        *,
        expected_generation: int,
    ) -> Applied[ParticipantSnapshot]:
        with self.session.writing() as db:
            person = self.get(lookup)
            if not person.committed:
                raise IdentityConflict("uncommitted participant cannot own a generation")
            if person.owner_identity.generation != expected_generation:
                raise StaleRevision("owner generation changed")
            OwnerGenerations.update(
                db,
                where="owner_lookup=? AND generation=?",
                parameters=(lookup, expected_generation),
                generation=expected_generation + 1,
            )
            return Applied(self.get(lookup))


class ParticipantRows(AttachedPracticeRebuild):
    """Keep whole participant answers inside the existing selected-file closure."""

    def retain_referenced_rows(self, schema: type[TableSchema]) -> int:
        changed = super().retain_referenced_rows(schema)
        return changed + ParticipantStore.retain_owners(self, tuple(
            person.participant_lookup for person in Participants.select(self.destination)
        ))
