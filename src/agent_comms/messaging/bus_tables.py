"""Bus query projections over the message-owned record and capability declarations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import batched
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from agent_comms.errors import RelationViolationError
from agent_comms.messaging.bus_projection_tables import BusDerivedTables, BusRouteIdentity
from agent_comms.messaging.channel_targets import NamedTarget
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.message_send_reads import PublishedMessageRead
from agent_comms.messaging.messages import (
    Addressed,
    BusProjectionTables,
    BusRecord,
    BusRow,
    BusTables,
    MessageInput,
    MessageKind,
)
from agent_comms.messaging.projection_source import ProducedRow
from agent_comms.messaging.read_basis import RepliesDisplayScope
from agent_comms.nominal.class_fact import ClassFact
from agent_comms.nominal.field_codec import FieldCodec, MessageId, ThreadLookup
from agent_comms.storage.recorded_time import Timestamped
from agent_comms.storage.typed_table import (
    Column,
    ComposedSchemas,
    ExpressionIndex,
    Index,
    ProjectionProducerTables,
    RowNotFoundError,
    TableReference,
    TableSource,
    TypedRow,
)

if TYPE_CHECKING:
    from agent_comms.registry.registry_provenance import RegistryProvenance


@dataclass(frozen=True)
class ReferenceMatch(TypedRow):
    """The identity join's computed result, not another stored reference."""

    matched: bool


class BusSchema(ComposedSchemas, BusTables, ProjectionProducerTables, BusProjectionTables,
                declared_name=None):
    """One file reconciles its declared families using each family's carry policy."""


@dataclass(frozen=True)
class ParticipantSource(BusDerivedTables, declared_name=None):
    """A recorded participant's positions in actual frozen deliveries."""

    lookup: Annotated[ThreadLookup, Column(primary_key=True)]
    seq: Annotated[int, Column(primary_key=True)]

    @classmethod
    def query(cls, lookup: ThreadLookup) -> tuple[str, tuple[object, ...]]:
        return f'SELECT seq FROM "{cls.declared_name}" WHERE lookup=?', (lookup,)


class SentSource(ParticipantSource):
    """The participant authored this record."""


class ReceivedSource(ParticipantSource):
    """The participant belongs to the frozen delivery audience."""

    @classmethod
    def query(
        cls, lookup: ThreadLookup, sender_lookups: tuple[ThreadLookup, ...] = (),
    ) -> tuple[str, tuple[object, ...]]:
        query, parameters = super().query(lookup)
        if sender_lookups:
            marks = ",".join("?" for _ in sender_lookups)
            query += (f' AND seq IN (SELECT seq FROM "{SentSource.declared_name}" '
                      f'WHERE lookup IN ({marks}))')
        return query, (*parameters, *sender_lookups)


@dataclass(frozen=True)
class BusRecipientRouteCounts(BusRouteIdentity):
    """Per-(recipient, route) delivery totals maintained at append time.

    Cold thread-list and sidebar unread queries previously scanned all rows of
    ``received_source`` joined to ``bus_route_counts`` (scaling with actors * history).
    Any recipient route whose ``[min_seq, max_seq]`` and ``min_timestamp`` sit strictly
    inside the requested window answers in O(1) without touching per-message rows; only
    routes split by a high-water mark or window boundary fall back to row scans."""

    @ClassFact
    def writes(cls: type[BusRecipientRouteCounts]) -> tuple[type[ProducedRow], ...]:
        return (BusRecipientRouteCounts,)

    lookup: ThreadLookup
    deliveries: int
    min_seq: int
    max_seq: int
    min_timestamp: float
    indexes: ClassVar = (
        ExpressionIndex(
            ("{lookup}", "{target}", "{sender}", "coalesce({sender_lookup}, '')"),
            unique=True,
        ),
        Index(("lookup", "target", "sender", "sender_lookup", "max_seq")),
    )

    @classmethod
    def from_delivery(cls, lookup: ThreadLookup, message) -> Self:
        return cls(
            message.target, message.sender, message.sender_lookup,
            lookup, 1, message.seq, message.seq, message.timestamp,
        )

    def insertion(self) -> tuple[str, tuple]:
        statement, values = self._insertion()
        statement += (
            ' ON CONFLICT ("lookup", "target", "sender", coalesce("sender_lookup", \'\'))'
            ' DO UPDATE SET'
            ' "deliveries"="deliveries"+excluded."deliveries",'
            ' "min_seq"=min("min_seq",excluded."min_seq"),'
            ' "max_seq"=max("max_seq",excluded."max_seq"),'
            ' "min_timestamp"=min("min_timestamp",excluded."min_timestamp")'
        )
        return statement, values


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
        cls, lookup: ThreadLookup, sender_lookups: tuple[ThreadLookup, ...] = (),
    ) -> tuple[str, tuple[object, ...]]:
        query, parameters = AwaitingAnswerSource.query(lookup, sender_lookups)
        return query + " AND starts_direct_turn=?", (*parameters, True)


@dataclass(frozen=True)
class MessageIdentitySource(MessageReference, BusDerivedTables):
    """A record's existing reference, searchable without recomputing its content hash."""

    @classmethod
    def practice_sequence(cls, db: sqlite3.Connection, source: TableSource) -> int:
        through = BusRow.last_seq(db, table_source=source)
        if through:
            expected = cls.require(db, table_source=source, seq=through)
            if cls.require(db, seq=through) != expected:
                raise RelationViolationError("Practice bus identity differs at its last sequence")
        return through

    sender: str
    indexes = (
        Index(("seq",), unique=True),
        Index(("message_id",)),
        Index(("sender", "message_id")),
        Index(("sender", "seq")),
    )

    @classmethod
    def require_matched(cls, matches: Iterable[ReferenceMatch], refusal: Exception) -> None:
        if not all(match.matched for match in matches):
            raise refusal

    @classmethod
    def reference_predicate(
        cls, references: tuple[MessageReference, ...],
    ) -> tuple[str, tuple[object, ...]]:
        plans = FieldCodec.field_plans(MessageReference)
        columns = cls._column_list(tuple(plan.name for plan in plans))
        selected = ', '.join('json_extract(value, ?)' for plan in plans)
        return (f'({columns}) IN (SELECT {selected} FROM json_each(?))',
                (*(f'$.{plan.key}' for plan in plans),
                 FieldCodec.compact([FieldCodec.encode(reference) for reference in references])))

    @classmethod
    def require_sequences(cls, db: sqlite3.Connection, sequences: tuple[int, ...]) -> None:
        matches = ReferenceMatch.read(db.execute(
            f'SELECT identity.seq IS NOT NULL AS matched FROM json_each(?) AS supplied '
            f'LEFT JOIN "{cls.declared_name}" AS identity ON identity.seq=supplied.value',
            (json.dumps(sequences),),
        ))
        cls.require_matched(matches, RowNotFoundError(
            f"Missing recorded {cls.declared_name} sequence",
        ))

    @classmethod
    def require_references(cls, db: sqlite3.Connection,
                           references: tuple[MessageReference, ...]) -> None:
        """Every supplied full reference is a recorded identity; neither seq nor id alone."""
        matches = ReferenceMatch.read(db.execute(
            "SELECT identity.seq IS NOT NULL AS matched FROM json_each(?) AS supplied "
            f'LEFT JOIN "{cls.declared_name}" AS identity '
            "ON identity.seq=json_extract(supplied.value,'$[0]') "
            "AND identity.message_id=json_extract(supplied.value,'$[1]')",
            (json.dumps(tuple((reference.seq, reference.message_id) for reference in references)),),
        ))
        cls.require_matched(matches, RelationViolationError(
            "Message reference does not match a recorded message identity"))

    @classmethod
    def answering(
        cls, db: sqlite3.Connection, references: tuple[MessageReference, ...],
        route: NamedTarget, snapshot: RegistryProvenance,
    ) -> tuple[MessageReference, ...]:
        """Validate a batch's identities and select its exact reply destination once."""
        predicate, parameters = route.answered_sql(
            Addressed.table._sql("addressed.{target}"), "identity.sender", snapshot,
        )
        query = (
            "WITH supplied AS (SELECT key AS position, "
            "json_extract(value,'$[0]') AS seq, json_extract(value,'$[1]') AS message_id "
            "FROM json_each(?)), selected AS ("
            "SELECT supplied.position, supplied.seq, supplied.message_id, "
            "identity.seq IS NOT NULL AND addressed.seq IS NOT NULL AS matched, "
            f"COALESCE(({predicate}),0) AS answers FROM supplied "
            f'LEFT JOIN "{cls.declared_name}" AS identity ON identity.seq=supplied.seq '
            "AND identity.message_id=supplied.message_id "
            f'LEFT JOIN "{Addressed.table.declared_name}" AS addressed '
            "ON addressed.seq=identity.seq) "
            "SELECT seq, message_id, matched FROM selected "
            "WHERE NOT matched OR answers ORDER BY position"
        )
        selected = MessageReference.joined(db.execute(query, (
            json.dumps(tuple((reference.seq, reference.message_id) for reference in references)),
            *parameters,
        )), ReferenceMatch)
        cls.require_matched(
            (match for _, match in selected),
            RelationViolationError("Message reference does not match its bus record"),
        )
        return tuple(reference for reference, _ in selected)


class RecordedAnswers:
    """The committed answer relation, independent of the current author roster."""

    def predicate(self) -> tuple[str, tuple[str, ...]]:
        return "1", ()


@dataclass(frozen=True)
class AuthoredAnswers(RecordedAnswers):
    """A query explicitly restricted to the authors the reader asks about."""

    names: frozenset[str]

    def predicate(self) -> tuple[str, tuple[str, ...]]:
        marks = ",".join("?" for _ in self.names)
        return (
            f'EXISTS (SELECT 1 FROM "{MessageIdentitySource.declared_name}" AS author '
            f'WHERE author.seq=answered.seq AND author.sender IN ({marks}))',
            tuple(self.names),
        )


@dataclass(frozen=True)
class AnsweredSource(BusDerivedTables):
    original: Annotated[int, Column(
        primary_key=True, references=TableReference(MessageIdentitySource, ("seq",)),
    )]
    seq: Annotated[int, Column(primary_key=True)]
    indexes = (Index(("original",)), Index(("seq", "original")))

    @classmethod
    def first_for_recipients(
        cls,
        db: sqlite3.Connection,
        keys: tuple[tuple[ThreadLookup, MessageReference], ...],
    ) -> tuple[RecipientReply, ...]:
        """The first reply's original identity, frozen author and clock, without its body."""
        selected = []
        for batch in batched(
            tuple(dict.fromkeys(keys)),
            db.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) // 3,
            strict=False,
        ):
            values = ",".join("(?,?,?)" for _ in batch)
            selected.extend(
                RecipientReply.read(
                    db.execute(
                        f"WITH selected(lookup,original,message_id) AS (VALUES {values}) "
                        "SELECT answer.original,answer.seq,author.lookup,reply.message_id,"
                        "original.message_id AS original_message_id,clock.timestamp FROM selected "
                        f"JOIN {MessageIdentitySource.declared_name} original "
                        "ON original.seq=selected.original AND original.message_id=selected.message_id "
                        f"JOIN {cls.declared_name} answer ON answer.original=selected.original "
                        f"AND answer.seq=(SELECT candidate.seq FROM {cls.declared_name} candidate "
                        f"CROSS JOIN {SentSource.declared_name} author "
                        "ON author.lookup=selected.lookup AND author.seq=candidate.seq "
                        "WHERE candidate.original=selected.original ORDER BY candidate.seq LIMIT 1) "
                        f"JOIN {SentSource.declared_name} author "
                        "ON author.seq=answer.seq AND author.lookup=selected.lookup "
                        f"JOIN {MessageIdentitySource.declared_name} reply ON reply.seq=answer.seq "
                        f"JOIN {MessageInput.table.declared_name} clock ON clock.seq=answer.seq",
                        tuple(
                            value
                            for lookup, reference in batch
                            for value in (lookup, reference.seq, reference.message_id)
                        ),
                    )
                )
            )
        return tuple(sorted(selected, key=lambda reply: reply.seq))

    @classmethod
    def unanswered_references(
        cls, db: sqlite3.Connection, sequences: tuple[int, ...], answers: RecordedAnswers,
    ) -> tuple[MessageReference, ...]:
        """Select exact originals against the requested committed answer relation."""
        if not sequences:
            return ()
        MessageIdentitySource.require_sequences(db, sequences)
        asking = ",".join("?" for _ in sequences)
        predicate, parameters = answers.predicate()
        identity = MessageIdentitySource.declared_name
        rows = db.execute(
            f'SELECT asked.seq, asked.message_id FROM "{identity}" AS asked '
            f'WHERE asked.seq IN ({asking}) AND NOT EXISTS ('
            f'SELECT 1 FROM "{cls.declared_name}" AS answered '
            + cls._sql('WHERE answered.{original}=asked.seq ')
            + f'AND ({predicate})) ORDER BY asked.seq',
            (*sequences, *parameters),
        )
        return tuple(MessageReference.iterate(rows))

    @classmethod
    def latest_reply(
        cls, db: sqlite3.Connection, sequences: tuple[int, ...], responders: frozenset[str],
    ) -> tuple[BusRecord, ...]:
        """The newest reply the responders sent naming any of those originals, from one read."""
        if not sequences:
            return ()
        asked = ",".join("?" for _ in sequences)
        replying = ",".join("?" for _ in responders)
        identity = MessageIdentitySource.declared_name
        rows = db.execute(
            f'SELECT answered.seq FROM "{cls.declared_name}" AS answered '
            f'JOIN "{identity}" AS author ON author.seq=answered.seq '
            + cls._sql(f'WHERE answered.{{original}} IN ({asked}) ')
            + f'AND author.sender IN ({replying}) ORDER BY answered.seq DESC LIMIT 1',
            (*sequences, *responders),
        )
        headers = (BusRow.require(db, seq=FieldCodec.decode(int, row[0])) for row in rows)
        return tuple(header.kind.read_from(db, header.seq) for header in headers)

    @classmethod
    def unanswered(
        cls, db: sqlite3.Connection, scope: RepliesDisplayScope,
        since: float, before: float, upper: int, limit: int,
        kinds: tuple[type[MessageKind], ...] = (),
    ) -> tuple[BusRecord, ...]:
        """Read unanswered originals through the existing reference and sender indexes."""
        requesters, responders = scope.second_names - scope.first_names, scope.first_names
        eligible, eligibility = AwaitingAnswerSource.recipient_predicate(
            "asked.seq", scope.first_names,
        )
        asking = ",".join("?" for _ in requesters)
        replying = ",".join("?" for _ in responders)
        kind_filter = (
            "AND " + MessageInput.table._sql("message.{type}")
            + " IN (" + ",".join("?" for _ in kinds) + ") "
            if kinds else ""
        )
        query = (
            f'SELECT asked.seq FROM "{MessageIdentitySource.declared_name}" AS asked '
            f'JOIN "{MessageInput.table.declared_name}" AS message USING(seq) '
            f'WHERE asked.sender IN ({asking}) AND '
            + MessageInput.table._sql('message.{timestamp} BETWEEN ? AND ? ')
            + f'AND asked.seq < ? {kind_filter}AND ({eligible}) AND NOT EXISTS ('
            f'SELECT 1 FROM "{cls.declared_name}" AS answered '
            + cls._sql('WHERE answered.{original}=asked.seq ')
            + f'AND EXISTS (SELECT 1 FROM "{MessageIdentitySource.declared_name}" AS author '
            f'WHERE author.seq=answered.seq AND author.sender IN ({replying}))) '
            'ORDER BY asked.seq DESC LIMIT ?'
        )
        sequences = db.execute(query, (
            *requesters, since, before, upper, *(kind.declared_name for kind in kinds),
            *eligibility, *responders, limit,
        ))
        headers = (BusRow.require(db, seq=FieldCodec.decode(int, row[0])) for row in sequences)
        return tuple(header.kind.read_from(db, header.seq) for header in headers)

    @classmethod
    def unanswered_for_debtors(
        cls,
        db: sqlite3.Connection,
        debtors: tuple[tuple[str, tuple[str, ...]], ...],
        requesters: tuple[str, ...],
        since: float,
        before: float,
        upper: int,
        limit: int,
        kinds: tuple[type[MessageKind], ...] = (),
    ) -> tuple[tuple[tuple[str, BusRecord], ...], bool]:
        """Read the complete debtor relation once, not the same question scan per debtor.

        Each debtor carries every registry name that can address or answer for it. The CTE
        retains that owner identity while joining the append-time recipient and exact-answer
        indexes, so aliases share one query without merging independently owed answers.
        """
        aliases = tuple(
            (debtor, alias)
            for debtor, names in debtors
            for alias in names
        )
        if not aliases or not requesters:
            return (), False
        values = ",".join("(?, ?)" for _ in aliases)
        asking = ",".join("?" for _ in requesters)
        kind_filter = (
            "AND " + MessageInput.table._sql("message.{type}")
            + " IN (" + ",".join("?" for _ in kinds) + ") "
            if kinds else ""
        )
        identity = MessageIdentitySource.declared_name
        rows = tuple(db.execute(
            f'WITH debtors(debtor, alias) AS (VALUES {values}) '
            'SELECT debtor, seq, page_position FROM ('
            'SELECT DISTINCT debtors.debtor AS debtor, asked.seq AS seq, '
            'DENSE_RANK() OVER (ORDER BY asked.seq DESC) AS page_position FROM debtors '
            f'JOIN "{AwaitingAnswerSource.declared_name}" AS awaiting '
            f'ON awaiting.recipient=debtors.alias '
            f'JOIN "{identity}" AS asked ON asked.seq=awaiting.seq '
            f'JOIN "{MessageInput.table.declared_name}" AS message USING(seq) '
            f'WHERE asked.sender IN ({asking}) AND '
            + MessageInput.table._sql('message.{timestamp} BETWEEN ? AND ? ')
            + f'AND asked.seq < ? {kind_filter}'
            + 'AND NOT EXISTS (SELECT 1 FROM debtors AS self '
              'WHERE self.debtor=debtors.debtor AND self.alias=asked.sender) '
            + f'AND NOT EXISTS (SELECT 1 FROM "{cls.declared_name}" AS answered '
              f'JOIN "{identity}" AS author ON author.seq=answered.seq '
            + cls._sql('WHERE answered.{original}=asked.seq ')
            + 'AND EXISTS (SELECT 1 FROM debtors AS responder '
              'WHERE responder.debtor=debtors.debtor AND responder.alias=author.sender)) '
            + ') WHERE page_position <= ? ORDER BY seq DESC',
            (
                *(value for pair in aliases for value in pair),
                *requesters,
                since,
                before,
                upper,
                *(kind.declared_name for kind in kinds),
                limit + 1,
            ),
        ))
        more = any(FieldCodec.decode(int, position) > limit for _, _, position in rows)
        positions = tuple(
            (FieldCodec.decode(str, debtor), FieldCodec.decode(int, sequence))
            for debtor, sequence, position in rows
            if FieldCodec.decode(int, position) <= limit
        )
        records = {
            sequence: header.kind.read_from(db, header.seq)
            for sequence in dict.fromkeys(sequence for _, sequence in positions)
            for header in (BusRow.require(db, seq=sequence),)
        }
        return tuple((debtor, records[sequence]) for debtor, sequence in positions), more


@dataclass(frozen=True)
class RecipientReply(AnsweredSource, SentSource, MessageReference, Timestamped, PublishedMessageRead,
                     declared_name=None):
    """A read-only join of answer, author, identity and original message clock; no stored copy."""

    original_message_id: MessageId
    timestamp: float

    @property
    def reference(self) -> MessageReference:
        return MessageReference(self.seq, self.message_id)

    @property
    def original_reference(self) -> MessageReference:
        return MessageReference(self.original, self.original_message_id)
