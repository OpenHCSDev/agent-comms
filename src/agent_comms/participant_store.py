"""Participant store: owned coordinator state and transitions."""

from __future__ import annotations

from dataclasses import dataclass

from agent_comms.coordination_contracts import (
    MAX_IDENTIFIER_CHARS,
)
from agent_comms.coordination_errors import (
    IdentityConflict,
    ParticipantNotRegistered,
    StaleRevision,
)
from agent_comms.coordination_results import AlreadyApplied, Applied
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordination_tables.executions import (
    CurrentExecutions,
    NoPointedAttempt,
    NoPointedExecution,
)
from agent_comms.coordination_tables.participants import (
    OwnerGenerations,
    Participants,
)
from agent_comms.field_codec import MalformedRecord, ThreadLookup


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


class ParticipantStore:
    def __init__(self, session: CoordinationSession) -> None:
        self.session = session

    def get(self, lookup: ThreadLookup) -> ParticipantSnapshot:
        with self.session.read():
            person = Participants.one(self.session._connection, participant_lookup=lookup)
            generation = OwnerGenerations.one(self.session._connection, owner_lookup=lookup)
            pointer = CurrentExecutions.one(self.session._connection, owner_lookup=lookup)
            if person is None or generation is None or pointer is None:
                raise ParticipantNotRegistered(f"participant {lookup!r} aggregate is not registered")
            return ParticipantSnapshot(
                lookup,
                person.committed,
                generation,
                pointer,
            )

    def register(
        self,
        lookup: ThreadLookup,
        *,
        committed: bool = False,
    ) -> Applied[ParticipantSnapshot] | AlreadyApplied[ParticipantSnapshot]:
        if type(committed) is not bool:
            raise ValueError("committed must be a boolean")
        if not isinstance(lookup, str) or not 1 <= len(lookup) <= MAX_IDENTIFIER_CHARS:
            raise MalformedRecord("participant identity must be bounded and nonempty")
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
