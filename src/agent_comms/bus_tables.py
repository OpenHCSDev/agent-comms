"""Bus field declarations, independent of Message and publication lifetimes."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import Field, dataclass, fields, make_dataclass
from functools import cache
from inspect import get_annotations
from typing import TYPE_CHECKING, Annotated, ClassVar, Self, get_origin, get_type_hints

from agent_comms import messages
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.envelope_claim_transitions import (
    ClaimProjection,
    ClaimTransition,
    ClaimTransitionError,
    ResourceClaim,
    UnclaimedResource,
)
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, WireName, WireOrder
from agent_comms.message_reference import MessageReference
from agent_comms.typed_table import (
    TableReference,
    AppendOnlyTables,
    Column,
    ComposedSchemas,
    DerivedTables,
    DurableTables,
    ForeignKey,
    Index,
    ProducedRow,
    ProjectionProducerTables,
    SharedFileTables,
    TypedRow,
    TypedTable,
    sql_literal,
)

if TYPE_CHECKING:
    from .bus_publication import Delivered
    from .read_basis import RepliesDisplayScope


class BusTables(SharedFileTables, DurableTables, AppendOnlyTables):
    """Bus history cannot be rebuilt from another source."""


class BusProjectionTables(ProducedRow, SharedFileTables, DerivedTables):
    """Reverse lookups are derived from the record, in its append transaction."""

    @classmethod
    def create_schema(cls, db: sqlite3.Connection) -> None:
        """Create/rebuild rows from history using the append projection owner."""
        super().create_schema(db)
        with closing(BusRow.stream(db, order_by=("seq",))) as headers:
            for header in headers:
                header.kind.read_from(db, header.seq).project_into(db)


class BusSchema(ComposedSchemas, BusTables, ProjectionProducerTables, BusProjectionTables):
    """One file reconciles its declared families using each family's carry policy."""


@dataclass(frozen=True)
class ParticipantSource(TypedTable, BusProjectionTables, declared_name=None):
    """A recorded participant's positions in actual frozen deliveries."""

    lookup: Annotated[str, Column(primary_key=True)]
    seq: Annotated[int, Column(primary_key=True)]

    @classmethod
    def query(cls, lookup: str) -> tuple[str, tuple[object, ...]]:
        return f'SELECT seq FROM "{cls.declared_name}" WHERE lookup=?', (lookup,)


class SentSource(ParticipantSource):
    """The participant authored this record."""


class ReceivedSource(ParticipantSource):
    """The participant belongs to the frozen delivery audience."""

    @classmethod
    def query(
        cls, lookup: str, sender_lookups: tuple[str, ...] = (),
    ) -> tuple[str, tuple[object, ...]]:
        query, parameters = super().query(lookup)
        if sender_lookups:
            marks = ",".join("?" for _ in sender_lookups)
            query += (f' AND seq IN (SELECT seq FROM "{SentSource.declared_name}" '
                      f'WHERE lookup IN ({marks}))')
        return query, (*parameters, *sender_lookups)


@dataclass(frozen=True)
class AwaitingAnswerSource(ReceivedSource):
    """The scope owner's append-time answer eligibility for a frozen recipient."""

    recipient: str
    starts_direct_turn: bool
    renamed_from = ("direct_reply_source",)
    indexes = (
        Index(("recipient", "seq")),
        Index(("lookup", "starts_direct_turn", "seq")),
    )

    @classmethod
    def recipient_predicate(
        cls, sequence: str, recipients: frozenset[str],
    ) -> tuple[str, tuple[str, ...]]:
        marks = ",".join("?" for _ in recipients)
        return (
            f'EXISTS (SELECT 1 FROM "{cls.declared_name}" AS awaiting '
            f'WHERE awaiting.seq={sequence} AND awaiting.recipient IN ({marks}))',
            tuple(recipients),
        )


class DirectReplySource(AwaitingAnswerSource, declared_name=None):
    """A bounded selection of the same answer index, with no second stored table."""

    @classmethod
    def query(
        cls, lookup: str, sender_lookups: tuple[str, ...] = (),
    ) -> tuple[str, tuple[object, ...]]:
        query, parameters = AwaitingAnswerSource.query(lookup, sender_lookups)
        return query + " AND starts_direct_turn=?", (*parameters, True)


@dataclass(frozen=True)
class MessageIdentitySource(MessageReference, TypedTable, BusProjectionTables):
    """A record's existing reference, searchable without recomputing its content hash."""

    sender: str
    indexes = (
        Index(("seq",), unique=True),
        Index(("message_id",)),
        Index(("sender", "message_id")),
    )

    @classmethod
    def reference_sql(cls, alias: str) -> str:
        fields = ",".join(
            value for item, key in FieldCodec.record_fields(MessageReference)
            for value in (sql_literal(key), cls._sql(alias + ".{" + item.name + "}"))
        )
        return f"json_object({fields})"


@dataclass(frozen=True)
class AnsweredSource(TypedTable, BusProjectionTables):
    original: Annotated[MessageReference, Column(primary_key=True)]
    seq: Annotated[int, Column(primary_key=True)]

    @classmethod
    def unanswered_references(
        cls, db: sqlite3.Connection, sequences: tuple[int, ...], responders: frozenset[str],
    ) -> tuple[MessageReference, ...]:
        """Select recorded originals before loading any message or reply content."""
        if not sequences:
            return ()
        for seq in sequences:
            MessageIdentitySource.require(db, seq=seq)
        asking = ",".join("?" for _ in sequences)
        replying = ",".join("?" for _ in responders)
        identity = MessageIdentitySource.declared_name
        reference = MessageIdentitySource.reference_sql("asked")
        rows = db.execute(
            f'SELECT asked.seq, asked.message_id FROM "{identity}" AS asked '
            f'WHERE asked.seq IN ({asking}) AND NOT EXISTS ('
            f'SELECT 1 FROM "{cls.declared_name}" AS answered '
            f'JOIN "{identity}" AS author ON author.seq=answered.seq '
            + cls._sql(f'WHERE answered.{{original}}={reference} ')
            + f'AND author.sender IN ({replying})) ORDER BY asked.seq',
            (*sequences, *responders),
        )
        return tuple(MessageReference(FieldCodec.decode(int, row[0]),
                                      FieldCodec.decode(str, row[1])) for row in rows)

    @classmethod
    def unanswered(
        cls, db: sqlite3.Connection, scope: RepliesDisplayScope,
        since: float, before: float, upper: int, limit: int,
        kinds: tuple[type[messages.MessageKind], ...] = (),
    ) -> tuple[messages.BusRecord, ...]:
        """Read unanswered originals through the existing reference and sender indexes."""
        requesters, responders = scope.second_names - scope.first_names, scope.first_names
        reference = MessageIdentitySource.reference_sql("asked")
        eligible, eligibility = AwaitingAnswerSource.recipient_predicate(
            "asked.seq", scope.first_names,
        )
        asking = ",".join("?" for _ in requesters)
        replying = ",".join("?" for _ in responders)
        kind_filter = (
            "AND " + messages.Message.table._sql("message.{type}")
            + " IN (" + ",".join("?" for _ in kinds) + ") "
            if kinds else ""
        )
        query = (
            f'SELECT asked.seq FROM "{MessageIdentitySource.declared_name}" AS asked '
            f'JOIN "{messages.Message.table.declared_name}" AS message USING(seq) '
            f'WHERE asked.sender IN ({asking}) AND '
            + messages.Message.table._sql('message.{timestamp} BETWEEN ? AND ? ')
            + f'AND asked.seq < ? {kind_filter}AND ({eligible}) AND NOT EXISTS ('
            f'SELECT 1 FROM "{cls.declared_name}" AS answered '
            f'JOIN "{MessageIdentitySource.declared_name}" AS author ON author.seq=answered.seq '
            + cls._sql(f'WHERE answered.{{original}}={reference} ')
            + f'AND author.sender IN ({replying})) ORDER BY asked.seq DESC LIMIT ?'
        )
        sequences = db.execute(query, (
            *requesters, since, before, upper, *(kind.declared_name for kind in kinds),
            *eligibility, *responders, limit,
        ))
        headers = (BusRow.require(db, seq=FieldCodec.decode(int, row[0])) for row in sequences)
        return tuple(header.kind.read_from(db, header.seq) for header in headers)

    @classmethod
    def _answer_rows(
        cls, db: sqlite3.Connection, scope: RepliesDisplayScope,
        since: float, selection: str,
    ) -> sqlite3.Cursor:
        requesters, responders = scope.second_names, scope.first_names
        eligible, eligibility = AwaitingAnswerSource.recipient_predicate(
            "asked.seq", scope.first_names,
        )
        asking = ",".join("?" for _ in requesters)
        replying = ",".join("?" for _ in responders)
        identity = MessageIdentitySource.declared_name
        content = messages.Message.table
        query = (
            'WITH matches AS (SELECT reply.seq, '
            + content._sql('reply_message.{timestamp} AS timestamp ')
            + f'FROM "{identity}" AS reply '
            f'JOIN "{content.declared_name}" AS reply_message USING(seq) '
            f'WHERE reply.sender IN ({replying}) AND '
            + content._sql('reply_message.{timestamp} >= ? AND EXISTS (')
            + f'SELECT 1 FROM "{cls.declared_name}" AS answered '
            f'JOIN "{identity}" AS asked ON '
            + cls._sql(f'answered.{{original}}={MessageIdentitySource.reference_sql("asked")} ')
            + f'JOIN "{content.declared_name}" AS question ON question.seq=asked.seq '
            f'WHERE answered.seq=reply.seq AND asked.seq<reply.seq '
            f'AND asked.sender IN ({asking}) AND '
            + content._sql('question.{timestamp} >= ? ')
            + f'AND ({eligible}))) ' + selection
        )
        return db.execute(query, (*responders, since, *requesters, since, *eligibility))

    @classmethod
    def latest_answer(
        cls, db: sqlite3.Connection, scope: RepliesDisplayScope,
        since: float,
    ) -> tuple[messages.BusRecord, ...]:
        sequences = cls._answer_rows(
            db, scope, since,
            'SELECT seq FROM matches ORDER BY seq DESC LIMIT 1',
        )
        headers = (BusRow.require(db, seq=FieldCodec.decode(int, row[0])) for row in sequences)
        return tuple(header.kind.read_from(db, header.seq) for header in headers)

    @classmethod
    def answer_timestamp(
        cls, db: sqlite3.Connection, scope: RepliesDisplayScope,
        since: float,
    ) -> float:
        row = cls._answer_rows(
            db, scope, since,
            'SELECT COALESCE(MAX(timestamp),0.0) FROM matches',
        ).fetchone()
        return FieldCodec.decode(float, row[0])


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
        return (ForeignKey(columns=("seq",), target=messages.Claimed.table, target_columns=("seq",)),)

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
        return (ForeignKey(columns=("claim_seq",), target=messages.Claimed.table, target_columns=("seq",)),)

    @classmethod
    def owner_for(cls, db: sqlite3.Connection, resource: str) -> ResourceClaim:
        originals = messages.Claimed.table.select(
            db, where=f'{{seq}}=(SELECT claim_seq FROM "{cls.declared_name}" WHERE resource=?)',
            parameters=(resource,), row_type=messages.Claimed,
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
        transitions = {row.claim_transition.seq: row.claim_transition for row in claim_table.select(
            db, where=f'{{seq}} IN ({marks})', parameters=sequences, row_type=messages.Claimed,
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


@dataclass(frozen=True)
class CapabilityRow(TypedTable, BusTables, declared_name=None):
    """Every generated capability row belongs to one original bus sequence."""

    seq: Annotated[int, Column(primary_key=True, references=TableReference(BusRow, ("seq",)))]


class _CapabilityTable:
    """A class's table is derived once, after its dataclass decorator has run."""

    def __get__(self, instance: BusCapability | None, owner: type[BusCapability]) -> type[CapabilityRow]:
        return self.for_class(owner)

    @cache
    def for_class(self, owner: type[BusCapability]) -> type[CapabilityRow]:
        hints = get_type_hints(owner, include_extras=True)
        return make_dataclass(
            BusTables.__name__.removesuffix("Tables") + owner.__name__ + "Row",
            [*((item.name, hints[item.name], item) for item in BusCapability.declared_fields(owner)),
             *((name, hints[name], vars(owner)[name]) for name in get_annotations(owner)
               if get_origin(hints[name]) is ClassVar and name in vars(owner))],
            bases=(CapabilityRow,),
            namespace={"__module__": owner.__module__},
            frozen=True,
        )


@dataclass(frozen=True)
class BusCapability(TypedRow):
    """The hierarchy and each dataclass declaration own their table union."""

    table = _CapabilityTable()

    def deliveries(self) -> tuple[Delivered, ...]:
        return ()

    @classmethod
    def declared_fields(cls, owner: type[BusCapability]) -> tuple[Field, ...]:
        own = get_annotations(owner)
        header_fields = {item.name for item in fields(BusRow)}
        return tuple(item for item in fields(owner)
                     if item.name in own and item.name not in header_fields)

    @classmethod
    @cache
    def tables(cls) -> tuple[type[CapabilityRow], ...]:
        return tuple(base.table for base in cls.__mro__
                     if issubclass(base, BusCapability) and BusCapability.declared_fields(base))

    def rows(self) -> tuple[CapabilityRow, ...]:
        """Project declared Python fields without flattening their wire representations."""
        return tuple(
            FieldCodec.construct(table, {
                item.name: getattr(self, item.name) for item in fields(table)
            })
            for table in self.tables())

    @classmethod
    def read_from(cls, db: sqlite3.Connection, seq: int) -> Self:
        """Reconstruct this declared value from its own capability tables."""
        values = {}
        for table in cls.tables():
            rows = table.select(db, seq=seq)
            if not rows:
                raise RelationViolationError(f"Bus row {seq} lacks {table.declared_name}")
            values.update({item.name: getattr(rows[0], item.name) for item in fields(table)})
        return FieldCodec.construct(cls, values)

    def projections(self) -> Iterator[TypedTable]:
        return iter(())

    def project_into(self, db: sqlite3.Connection) -> None:
        for row in self.projections():
            row.insert(db)

@dataclass(frozen=True)
class Addressed(BusCapability):
    sender: Annotated[str, WireName("from"), WireOrder(2)]
    target: Annotated[str, WireName("to"), WireOrder(3), Column(index=True)]
    indexes: ClassVar[tuple[Index, ...]] = (Index(("sender", "target", "seq")),)
