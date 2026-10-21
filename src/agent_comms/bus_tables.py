"""Bus field declarations, independent of Message and publication lifetimes."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import Field, dataclass, fields, make_dataclass, replace
from functools import cache
from inspect import get_annotations
from typing import Annotated, get_type_hints

from agent_comms import messages
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.envelope_claim_transitions import (
    ClaimProjection, ClaimTransition, ClaimTransitionError, ResourceClaim, UnclaimedResource,
)
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, WireName, WireOrder
from agent_comms.response_acknowledgement import KeyedResponseAcknowledgement
from agent_comms.typed_table import AppendOnlyTables, Column, DerivedTables, DurableTables, ForeignKey, Index, SharedFileTables, TypedTable
from agent_comms.message_reference import MessageReference


class BusTables(SharedFileTables, DurableTables, AppendOnlyTables):
    """Bus history cannot be rebuilt from another source."""


class BusProjectionTables(SharedFileTables, DerivedTables):
    """Reverse lookups are derived from the record, in its append transaction."""


@dataclass(frozen=True)
class ParticipantSource(TypedTable, BusProjectionTables, declared_name=None):
    lookup: Annotated[str, Column(primary_key=True)]
    seq: Annotated[int, Column(primary_key=True)]


class SentSource(ParticipantSource):
    """The participant authored this record."""


class ReceivedSource(ParticipantSource):
    """The participant belongs to the frozen delivery audience."""


class DirectReplySource(ReceivedSource):
    """The record's declared reply policy starts a separate direct turn."""


@dataclass(frozen=True)
class MessageIdentitySource(MessageReference, TypedTable, BusProjectionTables):
    """A record's existing reference, searchable without recomputing its content hash."""

    sender: str
    indexes = (
        Index(("seq",), unique=True),
        Index(("message_id",)),
        Index(("sender", "message_id")),
    )


@dataclass(frozen=True)
class AnsweredSource(TypedTable, BusProjectionTables):
    original: Annotated[MessageReference, Column(primary_key=True)]
    seq: Annotated[int, Column(primary_key=True)]


@dataclass(frozen=True)
class BusRow(TypedTable, BusTables):
    seq: Annotated[int, Column(primary_key=True, auto_increment=True)]
    kind: type[messages.BusRecord]
    at: float

    @classmethod
    def last_seq(cls, db: sqlite3.Connection) -> int:
        return FieldCodec.decode(int, db.execute(
            cls._sql(f'SELECT COALESCE(MAX({{seq}}), 0) FROM "{cls.declared_name}"')
        ).fetchone()[0])

    @classmethod
    def next_seq(cls, db: sqlite3.Connection) -> int:
        return FieldCodec.decode(int, db.execute(
            "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name=?), 0) + 1",
            (cls.declared_name,),
        ).fetchone()[0])


@dataclass(frozen=True)
class ClaimOperations(TypedTable, BusProjectionTables):
    """An acquisition's consumed operation refers to its original claim row."""

    operation_id: Annotated[str, Column(primary_key=True)]
    seq: int

    @classmethod
    def references(cls) -> tuple[ForeignKey, ...]:
        return (ForeignKey(("seq",), messages.Claimed.table, ("seq",)),)

    @classmethod
    def used(cls, db: sqlite3.Connection, operation_id: str) -> bool:
        return cls.exists(db, where="{operation_id} = ?", parameters=(operation_id,))


@dataclass(frozen=True)
class HeldResources(TypedTable, BusProjectionTables):
    """Current membership references the canonical transition, never a copy."""

    resource: Annotated[str, Column(primary_key=True)]
    claim_seq: int

    @classmethod
    def references(cls) -> tuple[ForeignKey, ...]:
        return (ForeignKey(("claim_seq",), messages.Claimed.table, ("seq",)),)

    @classmethod
    def owner_for(cls, db: sqlite3.Connection, resource: str) -> ResourceClaim:
        originals = messages.Claimed.table.select(
            db, where=f'{{seq}}=(SELECT claim_seq FROM "{cls.declared_name}" WHERE resource=?)',
            parameters=(resource,),
        )
        return next((row.claim_transition.claimed(resource) for row in originals), UnclaimedResource())

    @classmethod
    def all_claims(cls, db: sqlite3.Connection) -> ClaimProjection:
        return cls._project(db, cls.select(db), BusRow.last_seq(db))

    @classmethod
    def claims_for(
        cls, db: sqlite3.Connection, resources: tuple[str, ...], through: int,
    ) -> ClaimProjection:
        marks = ",".join("?" for _ in resources)
        return cls._project(db, cls.select(
            db, where=f'{{resource}} IN ({marks})', parameters=resources,
        ), through)

    @classmethod
    def _project(
        cls, db: sqlite3.Connection, current: list[HeldResources], through: int,
    ) -> ClaimProjection:
        claim_table = messages.Claimed.table
        last_seq = FieldCodec.decode(int, db.execute(claim_table._sql(
            f'SELECT COALESCE(MAX({{seq}}), 0) FROM "{claim_table.declared_name}" WHERE {{seq}} <= ?'
        ), (through,)).fetchone()[0])
        sequences = tuple({row.claim_seq for row in current})
        marks = ",".join("?" for _ in sequences)
        transitions = {row.seq: row.claim_transition for row in claim_table.select(
            db, where=f'{{seq}} IN ({marks})', parameters=sequences,
        )}
        return ClaimProjection(last_seq, {
            row.resource: transitions[row.claim_seq].claimed(row.resource) for row in current
        })

    @classmethod
    def project(cls, db: sqlite3.Connection, transition: ClaimTransition) -> ClaimProjection:
        """Validate before the original is inserted; the caller retains the writer."""
        try:
            transition.require_new_operation(lambda operation: ClaimOperations.used(db, operation))
            return transition.project(cls.claims_for(db, transition.resources, transition.seq - 1))
        except (ClaimTransitionError, IdentityConflict) as error:
            raise RelationViolationError("Committed claim transition is inconsistent.") from error

    @classmethod
    def apply(
        cls, db: sqlite3.Connection, transition: ClaimTransition, projected: ClaimProjection,
    ) -> None:
        removed = transition.resources + transition.expired
        marks = ",".join("?" for _ in removed)
        cls.delete(db, where=f'{{resource}} IN ({marks})', parameters=removed)
        for owner in projected.values():
            cls(owner.resource, owner.seq).insert(db)
        for operation_id in transition.operation_ids:
            ClaimOperations(operation_id, transition.seq).insert(db)


class _CapabilityTable:
    """A class's table is derived once, after its dataclass decorator has run."""

    def __get__(self, instance, owner) -> type[TypedTable]:
        return self.for_class(owner)

    @cache
    def for_class(self, owner) -> type[TypedTable]:
        hints = get_type_hints(owner, include_extras=True)
        return make_dataclass(
            BusTables.__name__.removesuffix("Tables") + owner.__name__ + "Row",
            [("seq", Annotated[int, Column(primary_key=True, references=(BusRow, "seq"))]),
             *((item.name, hints[item.name], item) for item in BusCapability.declared_fields(owner))],
            bases=(TypedTable, BusTables),
            namespace={"__module__": owner.__module__},
            frozen=True,
        )


@dataclass(frozen=True)
class BusCapability(BusTables):
    """The hierarchy and each dataclass declaration own their table union."""

    table = _CapabilityTable()

    def deliveries(self) -> tuple:
        return ()

    @classmethod
    @cache
    def declared_fields(cls, owner: type) -> tuple[Field, ...]:
        own = get_annotations(owner)
        header_fields = {item.name for item in fields(BusRow)}
        return tuple(item for item in fields(owner)
                     if item.name in own and item.name not in header_fields)

    @classmethod
    @cache
    def tables(cls) -> tuple[type[TypedTable], ...]:
        return tuple(base.table for base in cls.__mro__
                     if issubclass(base, BusCapability) and BusCapability.declared_fields(base))

    def rows(self) -> tuple[TypedTable, ...]:
        """Project declared Python fields without flattening their wire representations."""
        return tuple(
            FieldCodec.construct(table, {
                item.name: getattr(self, item.name) for item in fields(table)
            })
            for table in self.tables())

    def projections(self):
        return iter(())

    def project_into(self, db: sqlite3.Connection) -> None:
        for row in self.projections():
            row.insert(db)

    def append_to(self, db: sqlite3.Connection, seq: int) -> int:
        """The caller's transaction owns the sequence, including preserved history."""
        BusRow.insert_values(db, seq=seq, kind=type(self), at=time.time())
        stored = replace(self, seq=seq)
        for row in stored.rows():
            row.insert(db)
        stored.project_into(db)
        return stored.seq


@dataclass(frozen=True)
class Addressed(BusCapability):
    sender: Annotated[str, WireName("from"), WireOrder(2)]
    target: Annotated[str, WireName("to"), WireOrder(3), Column(index=True)]


@dataclass(frozen=True)
class KeyedPublication(BusCapability):
    acknowledgement: Annotated[KeyedResponseAcknowledgement, Column(unique=True)]

    def append_to(self, db: sqlite3.Connection, seq: int) -> int:
        """A keyed publication appended again answers with the sequence it first got."""
        prior = KeyedPublication.table.select(db, acknowledgement=self.acknowledgement)
        return prior[0].seq if prior else super().append_to(db, seq)
