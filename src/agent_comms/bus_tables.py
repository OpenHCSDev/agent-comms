"""Bus query projections over the message-owned record and capability declarations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from agent_comms import messages
from agent_comms.errors import RelationViolationError
from agent_comms.nominal.field_codec import FieldCodec, ThreadLookup
from agent_comms.message_reference import MessageReference
from agent_comms.typed_table import (
    Column,
    ComposedSchemas,
    Index,
    ProjectionProducerTables,
    RowNotFoundError,
    TableReference,
    TypedRow,
    TypedTable,
)

if TYPE_CHECKING:
    from .channel_targets import NamedTarget
    from .read_basis import RepliesDisplayScope
    from .registry_provenance import RegistryProvenance


@dataclass(frozen=True)
class ReferenceMatch(TypedRow):
    """The identity join's computed result, not another stored reference."""

    matched: bool


class BusSchema(ComposedSchemas, messages.BusTables, ProjectionProducerTables, messages.BusProjectionTables,
                declared_name=None):
    """One file reconciles its declared families using each family's carry policy."""


@dataclass(frozen=True)
class ParticipantSource(messages.BusProjectionTables, declared_name=None):
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
        cls, lookup: ThreadLookup, sender_lookups: tuple[str, ...] = (),
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
        cls, lookup: ThreadLookup, sender_lookups: tuple[str, ...] = (),
    ) -> tuple[str, tuple[object, ...]]:
        query, parameters = AwaitingAnswerSource.query(lookup, sender_lookups)
        return query + " AND starts_direct_turn=?", (*parameters, True)


@dataclass(frozen=True)
class MessageIdentitySource(MessageReference, messages.BusProjectionTables):
    """A record's existing reference, searchable without recomputing its content hash."""

    @classmethod
    def practice_sequence(cls, db: sqlite3.Connection, source: sqlite3.Connection) -> int:
        through = messages.BusRow.last_seq(source)
        if through:
            expected = cls.require(source, seq=through)
            if cls.require(db, seq=through) != expected:
                raise RelationViolationError("Practice bus identity differs at its last sequence")
        return through

    sender: str
    indexes = (
        Index(("seq",), unique=True),
        Index(("message_id",)),
        Index(("sender", "message_id")),
    )

    @classmethod
    def require_matched(cls, matches: Iterable[ReferenceMatch], refusal: Exception) -> None:
        if not all(match.matched for match in matches):
            raise refusal

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
            messages.Addressed.table._sql("addressed.{target}"), "identity.sender", snapshot,
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
            f'LEFT JOIN "{messages.Addressed.table.declared_name}" AS addressed '
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


@dataclass(frozen=True)
class AnsweredSource(messages.BusProjectionTables):
    original: Annotated[int, Column(
        primary_key=True, references=TableReference(MessageIdentitySource, ("seq",)),
    )]
    seq: Annotated[int, Column(primary_key=True)]
    indexes = (Index(("original",)), Index(("seq", "original")))

    @classmethod
    def unanswered_references(
        cls, db: sqlite3.Connection, sequences: tuple[int, ...], responders: frozenset[str],
    ) -> tuple[MessageReference, ...]:
        """Select recorded originals before loading any message or reply content."""
        if not sequences:
            return ()
        MessageIdentitySource.require_sequences(db, sequences)
        asking = ",".join("?" for _ in sequences)
        replying = ",".join("?" for _ in responders)
        identity = MessageIdentitySource.declared_name
        rows = db.execute(
            f'SELECT asked.seq, asked.message_id FROM "{identity}" AS asked '
            f'WHERE asked.seq IN ({asking}) AND NOT EXISTS ('
            f'SELECT 1 FROM "{cls.declared_name}" AS answered '
            f'JOIN "{identity}" AS author ON author.seq=answered.seq '
            + cls._sql('WHERE answered.{original}=asked.seq ')
            + f'AND author.sender IN ({replying})) ORDER BY asked.seq',
            (*sequences, *responders),
        )
        return tuple(MessageReference.iterate(rows))

    @classmethod
    def unanswered(
        cls, db: sqlite3.Connection, scope: RepliesDisplayScope,
        since: float, before: float, upper: int, limit: int,
        kinds: tuple[type[messages.MessageKind], ...] = (),
    ) -> tuple[messages.BusRecord, ...]:
        """Read unanswered originals through the existing reference and sender indexes."""
        requesters, responders = scope.second_names - scope.first_names, scope.first_names
        eligible, eligibility = AwaitingAnswerSource.recipient_predicate(
            "asked.seq", scope.first_names,
        )
        asking = ",".join("?" for _ in requesters)
        replying = ",".join("?" for _ in responders)
        kind_filter = (
            "AND " + messages.MessageInput.table._sql("message.{type}")
            + " IN (" + ",".join("?" for _ in kinds) + ") "
            if kinds else ""
        )
        query = (
            f'SELECT asked.seq FROM "{MessageIdentitySource.declared_name}" AS asked '
            f'JOIN "{messages.MessageInput.table.declared_name}" AS message USING(seq) '
            f'WHERE asked.sender IN ({asking}) AND '
            + messages.MessageInput.table._sql('message.{timestamp} BETWEEN ? AND ? ')
            + f'AND asked.seq < ? {kind_filter}AND ({eligible}) AND NOT EXISTS ('
            f'SELECT 1 FROM "{cls.declared_name}" AS answered '
            f'JOIN "{MessageIdentitySource.declared_name}" AS author ON author.seq=answered.seq '
            + cls._sql('WHERE answered.{original}=asked.seq ')
            + f'AND author.sender IN ({replying})) ORDER BY asked.seq DESC LIMIT ?'
        )
        sequences = db.execute(query, (
            *requesters, since, before, upper, *(kind.declared_name for kind in kinds),
            *eligibility, *responders, limit,
        ))
        headers = (messages.BusRow.require(db, seq=FieldCodec.decode(int, row[0])) for row in sequences)
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
        content = messages.MessageInput.table
        query = (
            'WITH matches AS (SELECT reply.seq, '
            + content._sql('reply_message.{timestamp} AS {timestamp} ')
            + f'FROM "{identity}" AS reply '
            f'JOIN "{content.declared_name}" AS reply_message USING(seq) '
            f'WHERE reply.sender IN ({replying}) AND '
            + content._sql('reply_message.{timestamp} >= ? AND EXISTS (')
            + f'SELECT 1 FROM "{cls.declared_name}" AS answered '
            f'WHERE answered.seq=reply.seq AND answered.original<reply.seq AND EXISTS ('
            f'SELECT 1 FROM "{identity}" AS asked '
            f'JOIN "{content.declared_name}" AS question ON question.seq=asked.seq '
            + cls._sql('WHERE asked.seq=answered.{original} ')
            + f'AND asked.sender IN ({asking}) AND '
            + content._sql('question.{timestamp} >= ? ')
            + f'AND ({eligible})))) ' + selection
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
        headers = (messages.BusRow.require(db, seq=FieldCodec.decode(int, row[0])) for row in sequences)
        return tuple(header.kind.read_from(db, header.seq) for header in headers)

    @classmethod
    def answer_timestamp(
        cls, db: sqlite3.Connection, scope: RepliesDisplayScope,
        since: float,
    ) -> float:
        row = cls._answer_rows(
            db, scope, since,
            messages.MessageInput.render('SELECT COALESCE(MAX({timestamp}),0.0) FROM matches'),
        ).fetchone()
        return FieldCodec.decode(float, row[0])
