"""Participant store: owned coordinator state and transitions."""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_comms.coordination_contracts import (
    MAX_IDENTIFIER_CHARS,
)
from agent_comms.coordination_errors import (
    IdentityConflict,
    StaleRevision,
)
from agent_comms.coordination_results import AlreadyApplied, Applied
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordination_tables.executions import (
    CurrentExecutions,
)
from agent_comms.coordination_tables.participants import (
    OwnerGenerations,
    ParticipantAliases,
    Participants,
)


@dataclass(frozen=True, slots=True)
class ParticipantSnapshot:
    """Durable coordinator assignment, independent of process admission.

    This generation begins at registration and advances on explicit coordinator
    reassignment/retry, even if the registry process has not changed. It must
    never be substituted for a Registration owner or admission generation.
    """

    lookup: str
    display_name: str
    committed: bool
    aliases: tuple[str, ...]
    owner_thread: str
    participant_generation: int = field(metadata={"wire_name": "generation"})
    pointer: CurrentExecutions


class ParticipantStore:
    def __init__(self, session: CoordinationSession) -> None:
        self.session = session

    def get(self, lookup: str) -> ParticipantSnapshot:
        with self.session.read():
            db = self.session._connection
            person = Participants.one(self.session._connection, participant_lookup=lookup)
            generation = OwnerGenerations.one(self.session._connection, owner_lookup=lookup)
            pointer = CurrentExecutions.one(self.session._connection, owner_lookup=lookup)
            if person is None or generation is None or pointer is None:
                raise IdentityConflict("participant aggregate is not registered")
            aliases = tuple(
                row.alias
                for row in ParticipantAliases.read(
                    db.execute(
                        f"SELECT * FROM {ParticipantAliases.declared_name} WHERE participant_lookup=? "
                        "ORDER BY renamed_at_ms, rowid",
                        (lookup,),
                    )
                )
            )
            return ParticipantSnapshot(
                lookup,
                person.display_name,
                person.committed,
                aliases,
                generation.owner_thread,
                generation.generation,
                pointer,
            )

    def register(
        self,
        lookup: str,
        display_name: str,
        owner_thread: str,
        *,
        alias: str | None = None,
        committed: bool = False,
    ) -> Applied[ParticipantSnapshot] | AlreadyApplied[ParticipantSnapshot]:
        if type(committed) is not bool:
            raise ValueError("committed must be a boolean")
        for value in (lookup, display_name, owner_thread, alias or lookup):
            if not isinstance(value, str) or not 1 <= len(value) <= MAX_IDENTIFIER_CHARS:
                raise ValueError("participant identity must be bounded and nonempty")
        primary = alias or lookup
        with self.session.transaction() as db:
            existing = Participants.one(self.session._connection, participant_lookup=lookup)
            if existing is not None:
                snapshot = self.get(lookup)
                # Commitment, display name and generation owner may advance.
                # The lookup and first accepted alias are the durable identity.
                # No mutable registration-row bundle becomes a fingerprint.
                if not snapshot.aliases or snapshot.aliases[0] != primary:
                    raise IdentityConflict("participant registration alias conflicts")
                return AlreadyApplied(snapshot)
            if ParticipantAliases.one(self.session._connection, alias=primary) is not None:
                raise IdentityConflict("alias already belongs to another participant")
            Participants(
                participant_lookup=lookup, display_name=display_name, committed=committed
            ).insert(db)
            ParticipantAliases(
                alias=primary, participant_lookup=lookup, renamed_at_ms=self.session.now()
            ).insert(db)
            OwnerGenerations(owner_lookup=lookup, owner_thread=owner_thread, generation=1).insert(
                db
            )
            CurrentExecutions(
                owner_lookup=lookup, execution_id=None, attempt_ordinal=None, pointer_revision=0
            ).insert(db)
            return Applied(self.get(lookup))

    def commit(
        self, lookup: str
    ) -> Applied[ParticipantSnapshot] | AlreadyApplied[ParticipantSnapshot]:
        with self.session.transaction() as db:
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

    def rename(
        self,
        lookup: str,
        alias: str,
        display_name: str,
        *,
        expected_generation: int,
    ) -> Applied[ParticipantSnapshot] | AlreadyApplied[ParticipantSnapshot]:
        for value in (alias, display_name):
            if not isinstance(value, str) or not 1 <= len(value) <= MAX_IDENTIFIER_CHARS:
                raise ValueError("alias and display name must be bounded")
        with self.session.transaction() as db:
            person = self.get(lookup)
            if person.participant_generation != expected_generation:
                raise StaleRevision("owner generation changed")
            existing = ParticipantAliases.one(self.session._connection, alias=alias)
            if existing is not None:
                if existing.participant_lookup != lookup:
                    raise IdentityConflict("alias is already assigned")
                return AlreadyApplied(person)
            last_alias_ms = max(
                row.renamed_at_ms
                for row in ParticipantAliases.select(
                    db, where="participant_lookup=?", parameters=(lookup,)
                )
            )
            ParticipantAliases(
                alias=alias,
                participant_lookup=lookup,
                renamed_at_ms=self.session.now(last_alias_ms),
            ).insert(db)
            Participants.update(
                db, where="participant_lookup=?", parameters=(lookup,), display_name=display_name
            )
            return Applied(self.get(lookup))

    def advance_generation(
        self,
        lookup: str,
        owner_thread: str,
        *,
        expected_generation: int,
    ) -> Applied[ParticipantSnapshot]:
        if not isinstance(owner_thread, str) or not 1 <= len(owner_thread) <= MAX_IDENTIFIER_CHARS:
            raise ValueError("owner thread must be bounded and nonempty")
        with self.session.transaction() as db:
            person = self.get(lookup)
            if not person.committed:
                raise IdentityConflict("uncommitted participant cannot own a generation")
            if person.participant_generation != expected_generation:
                raise StaleRevision("owner generation changed")
            OwnerGenerations.update(
                db,
                where="owner_lookup=? AND generation=?",
                parameters=(lookup, expected_generation),
                owner_thread=owner_thread,
                generation=expected_generation + 1,
            )
            return Applied(self.get(lookup))
