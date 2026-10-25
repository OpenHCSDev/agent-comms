"""Append-only database history and capability-declared record queries."""

from __future__ import annotations

from agent_comms.activity_clocks import ActivityClocks, ChannelActivity
import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager
from dataclasses import dataclass, replace
from heapq import nlargest
from itertools import islice, product
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.bus_activity_index import BusActivityIndex
from agent_comms.bus_display_index import BusDisplayIndex
from agent_comms.bus_publication import Delivered, KeyedPublication
from agent_comms.bus_route_counts import RouteCounts
from agent_comms.bus_tables import (
    AnsweredSource,
    BusSchema,
    DirectReplySource,
    MessageIdentitySource,
    ParticipantSource,
    ReceivedSource,
    SentSource,
)
from agent_comms.messages import (
    Addressed,
    BusRow,
    ClaimOperations,
    HeldResources,
    BusRecord,
    Claimed,
    ClaimMessage,
    Message,
    MessageKind,
    PublicRecord,
)
from agent_comms.envelope_claim_transitions import (
    ClaimOwner,
    ClaimProjection,
    ClaimTransition,
    RecordedScope,
    ResourceClaim,
)
from agent_comms.errors import CommittedMessageError, RelationViolationError, UnclaimedError
from agent_comms.field_codec import ClaimId, FieldCodec, MessageId, ThreadLookup
from agent_comms.locked_store import BusFile, SQLiteFile, StoreCommittedError
from agent_comms.message_reference import MessageReference
from agent_comms.private_path import FileIdentity, PresentPath
from agent_comms.read_basis import (
    DisplayUnreadSelection, MessageDisplayScope, ReadCursor, RepliesDisplayScope,
)
from agent_comms.store_files import UNBOUNDED_CONTENTION, StoreLockContention
from agent_comms.typed_table import CarriedWriter, ReconciledFile, SQLiteDatabase, WalStore
from agent_comms.watched import Watched

if TYPE_CHECKING:
    from agent_comms.registry_document import RegistrySnapshot
    from agent_comms.registry_provenance import RegistryProvenance
    from agent_comms.thread_identity import RecordedThreadIncarnation


class ClaimRecord(PublicRecord, ClaimMessage):
    pass


class PrivateRecord(Delivered):
    pass


class KeyedPrivateRecord(PrivateRecord, KeyedPublication):
    pass


type RecordBuilder = Callable[[int, sqlite3.Connection], BusRecord]


@dataclass(frozen=True)
class AddressedRow(Addressed, BusRow, declared_name=None):
    """A read-only join of the declared address and sequence; never another table."""

    @classmethod
    def matching(
        cls, db: sqlite3.Connection, predicate: str, parameters: tuple[object, ...],
    ) -> list[Self]:
        addresses = Addressed.table
        return cls.read(db.execute(
            f'SELECT {Addressed._select_list(addresses)}, {BusRow._select_list(BusRow)} '
            f'FROM "{addresses.declared_name}" '
            f'JOIN "{BusRow.declared_name}" USING(seq) '
            f'WHERE {addresses._sql(predicate)}', parameters))


    @classmethod
    def latest(
        cls, db: sqlite3.Connection, senders: frozenset[str], targets: frozenset[str],
    ) -> tuple[Message, ...]:
        """One indexed candidate per alias pair; an empty selection stays empty."""
        positions = nlargest(1, (
            row.seq for sender, target in product(senders, targets)
            for row in Addressed.table.select(
                db, sender=sender, target=target,
                order_by=("seq",), descending=True, limit=1,
            )
        ))
        headers = (BusRow.require(db, seq=seq) for seq in positions)
        return tuple(header.kind.read_from(db, header.seq) for header in headers)


class DisplayUnread:
    """Before the first count, acquire the complete unread selection."""

    def advanced(self, source: BusReader, selection: DisplayUnreadSelection) -> CountedDisplayUnread:
        return source.display_unread(selection, through=source.revision())


@dataclass(frozen=True)
class CountedDisplayUnread(DisplayUnread):
    """A counted selection advances only the newly appended interval."""

    through: int
    selection: DisplayUnreadSelection
    counts: Mapping[str, int]

    def advanced(self, source: BusReader, selection: DisplayUnreadSelection) -> CountedDisplayUnread:
        if self.selection != selection:
            return super().advanced(source, selection)
        through = source.revision()
        if through == self.through:
            return self
        counted = source.display_unread(
            selection, through=through, after=self.through,
        )
        return replace(counted, counts={
            channel: self.counts[channel] + count
            for channel, count in counted.counts.items()
        })


class BusReader(RouteCounts, ABC):
    """One query implementation for individual reads and a held read snapshot."""

    @abstractmethod
    def observing(
        self, contention: StoreLockContention = UNBOUNDED_CONTENTION,
    ) -> AbstractContextManager[sqlite3.Connection]:
        """One read snapshot; a file attachment spends ``contention``, the lock budget of
        the read it serves."""

    @contextmanager
    def reading(self) -> Iterator[BusSnapshot]:
        """Keep queries and their physical identity on one held snapshot."""
        with self.observing() as db:
            yield BusSnapshot.from_connection(db)

    def revision(self) -> int:
        with self.observing() as db:
            return BusRow.last_seq(db)

    def activity_clocks(
        self, contention: StoreLockContention = UNBOUNDED_CONTENTION,
    ) -> ActivityClocks:
        with self.observing(contention) as db:
            return BusActivityIndex.clocks(db)

    def target_activity(self, predicate: str, parameters: tuple[object, ...]) -> ChannelActivity:
        with self.observing() as db:
            return BusActivityIndex.aggregate(db, predicate, parameters)

    def participant_activity(self, names: frozenset[str]) -> ChannelActivity:
        marks = ','.join('?' for _ in names)
        with self.observing() as db:
            return BusDisplayIndex.aggregate(db, f'participant IN ({marks})', tuple(names))

    def claim_projection(self) -> ClaimProjection:
        with self.observing() as db:
            return HeldResources.all_claims(db)

    def resource_owner(self, resource: str) -> ResourceClaim:
        with self.observing() as db:
            return HeldResources.owner_for(db, resource)

    def claim_operation_used(self, operation_id: ClaimId) -> bool:
        with self.observing() as db:
            return ClaimOperations.used(db, operation_id)

    def claim(self, seq: int) -> ClaimTransition:
        """Read attestation from the row's capability, never a retained-history floor."""
        with self.observing() as db:
            rows = Claimed.read(db.execute(
                f'SELECT {Claimed._select_list(Claimed.table)} '
                f'FROM "{Claimed.table.declared_name}" WHERE seq=?', (seq,)))
            if not rows:
                raise UnclaimedError(seq)
            return rows[0].claim_transition

    def lookup(self, seq: int) -> BusRecord:
        with self.observing() as db:
            headers = BusRow.select(db, seq=seq)
            if not headers:
                raise RelationViolationError(f"Unknown bus sequence: {seq}")
            return headers[0].kind.read_from(db, headers[0].seq)

    def delivery(self, seq: int) -> Delivered:
        """A public history row cannot stand in for a frozen delivery."""
        for delivery in self.lookup(seq).deliveries():
            return delivery
        raise RelationViolationError("Bus sequence has no frozen delivery")

    def message_reference(self, message_id: MessageId) -> MessageReference:
        """Resolve one content identity; absent or ambiguous identities are refused."""
        with self.observing() as db:
            identity = MessageIdentitySource.require(db, message_id=message_id)
            return MessageReference(identity.seq, identity.message_id)

    def lookup_message(self, message_id: MessageId) -> BusRecord:
        with self.reading() as source:
            return source.lookup(source.message_reference(message_id).seq)

    def keyed_messages(self, publication_key: str) -> tuple[BusRecord, ...]:
        """Read at most the one original bound to this publication key."""
        with self.observing() as db:
            headers = (BusRow.require(db, seq=seq) for seq in
                       KeyedPublication.publication_sequences(db, publication_key))
            return tuple(header.kind.read_from(db, header.seq) for header in headers)

    def page(self, before: int, limit: int) -> tuple[BusRecord, ...]:
        with self.observing() as db:
            return tuple(row.kind.read_from(db, row.seq) for row in BusRow.select(
                db, where="{seq} < ?", parameters=(before,),
                order_by=("seq",), descending=True, limit=limit))

    def message_history(self) -> Iterator[Message]:
        """Public bulk export, paged within one committed SQLite snapshot."""
        with self.reading() as source:
            after, through = 0, source.revision()
            while after < through:
                records = source.since(after, 100)
                for record in records:
                    for message in record.messages():
                        yield Message.from_message(message)
                after = records[-1].seq

    def since(self, after: int, limit: int) -> tuple[BusRecord, ...]:
        """One ascending keyset page after the last scanned bus sequence."""
        with self.observing() as db:
            return tuple(row.kind.read_from(db, row.seq) for row in BusRow.select(
                db, where="{seq} > ?", parameters=(after,),
                order_by=("seq",), limit=limit))

    def inbox(self, target: str, after: int, limit: int) -> tuple[BusRecord, ...]:
        with self.observing() as db:
            rows = db.execute(
                f'SELECT seq FROM "{Addressed.table.declared_name}" '
                'WHERE target=? AND seq>? ORDER BY seq LIMIT ?', (target, after, limit))
            headers = (BusRow.require(db, seq=FieldCodec.decode(int, row[0])) for row in rows)
            return tuple(header.kind.read_from(db, header.seq) for header in headers)

    def scoped_page(
        self, predicate: str, parameters: tuple[object, ...], *, lower: int = 0,
        upper: int | None = None, descending: bool = False, limit: int = 100,
        kinds: tuple[type[MessageKind], ...] = (),
    ) -> tuple[Message, ...]:
        """A captured display scope narrows the addressed table before decoding."""
        with self.observing() as db:
            upper = BusRow.last_seq(db) + 1 if upper is None else upper
            order = "DESC" if descending else "ASC"
            members = ""
            if kinds:
                members = ' AND type IN (' + ','.join('?' for _ in kinds) + ')'
            rows = db.execute(
                f'SELECT seq FROM "{Addressed.table.declared_name}" '
                f'JOIN "{Message.table.declared_name}" USING(seq) '
                f'WHERE ({predicate}) AND seq > ? AND seq < ?{members} '
                f'ORDER BY seq {order} LIMIT ?',
                (*parameters, lower, upper, *(kind.declared_name for kind in kinds), limit),
            )
            headers = (BusRow.require(db, seq=FieldCodec.decode(int, row[0])) for row in rows)
            return tuple(header.kind.read_from(db, header.seq) for header in headers)

    def latest_conversations(
        self, predicate: str, parameters: tuple[object, ...], through: int,
    ) -> list[AddressedRow]:
        """One prefix per addressed conversation, without materializing its history."""
        with self.observing() as db:
            return AddressedRow.matching(
                db, (f'{{seq}} IN (SELECT MAX(seq) FROM '
                     f'"{Addressed.table.declared_name}" '
                     f'JOIN "{Message.table.declared_name}" USING(seq) '
                     f'WHERE ({predicate}) AND seq <= ? GROUP BY sender, target)'),
                (*parameters, through),
            )

    def latest_addressed(
        self, senders: frozenset[str], targets: frozenset[str],
    ) -> tuple[Message, ...]:
        with self.observing() as db:
            return AddressedRow.latest(db, senders, targets)

    def messages_for_references(
        self, references: Iterable[MessageReference],
    ) -> tuple[Message, ...]:
        references = tuple(references)
        with self.reading() as source:
            records = tuple(source.lookup(reference.seq) for reference in references)
            messages = tuple(message for record in records for message in record.messages())
        if tuple(message.reference for message in messages) != references:
            raise RelationViolationError("Message reference does not match its bus record")
        return messages

    def deliveries_for_references(
        self, references: Iterable[MessageReference],
    ) -> tuple[Delivered, ...]:
        return tuple(delivery for record in self.messages_for_references(references)
                     for delivery in record.deliveries())

    def _source_sequences(
        self, query: str, parameters: tuple[object, ...], *, after: int = 0,
        before: int | None = None, limit: int = 100, descending: bool = True,
        sequences: tuple[int, ...] = (),
    ) -> Generator[int]:
        """Stream selected frozen participant positions on the held snapshot."""
        with self.observing() as db:
            before = BusRow.last_seq(db) + 1 if before is None else before
            selected = ""
            if sequences:
                selected = " AND seq IN (" + ",".join("?" for _ in sequences) + ")"
            order = "DESC" if descending else "ASC"
            with closing(db.execute(
                f"SELECT seq FROM ({query}) WHERE seq > ? AND seq < ?{selected} "
                f"ORDER BY seq {order} LIMIT ?",
                (*parameters, after, before, *sequences, limit),
            )) as rows:
                for row in rows:
                    yield FieldCodec.decode(int, row[0])

    def conversation(
        self, lookup: ThreadLookup, *, after: int = 0, before: int | None = None, limit: int = 100,
        descending: bool = True, sequences: tuple[int, ...] = (),
    ) -> tuple[Delivered, ...]:
        with self.reading() as source:
            positions: set[int] = set()
            for participant in ParticipantSource.schema_rows():
                with closing(source._source_sequences(
                    *participant.query(lookup), after=after, before=before, descending=descending,
                    sequences=sequences, limit=limit,
                )) as selected:
                    positions.update(selected)
            return tuple(source.delivery(seq) for seq in islice(
                sorted(positions, reverse=descending), limit,
            ))

    def sent(
        self, lookup: ThreadLookup, *, after: int = 0, before: int | None = None,
        limit: int = 100, descending: bool = True,
    ) -> tuple[Delivered, ...]:
        with self.reading() as source, closing(source._source_sequences(
            *SentSource.query(lookup),
            after=after, before=before, descending=descending, limit=limit,
        )) as positions:
            return tuple(source.delivery(seq) for seq in positions)

    def received(
        self, lookup: ThreadLookup, *, after: int = 0, before: int | None = None, limit: int = 100,
        sender_lookups: tuple[str, ...] = (), descending: bool = True,
        sequences: tuple[int, ...] = (),
    ) -> tuple[Delivered, ...]:
        with self.reading() as source, closing(source._source_sequences(
            *ReceivedSource.query(lookup, sender_lookups), after=after, before=before,
            descending=descending, sequences=sequences, limit=limit,
        )) as positions:
            return tuple(source.delivery(seq) for seq in positions)

    def direct_replies(
        self, lookup: ThreadLookup, *, after: int = 0, limit: int = 100,
        sender_lookups: tuple[str, ...] = (), descending: bool = True,
        sequences: tuple[int, ...] = (),
    ) -> tuple[Delivered, ...]:
        """Read the bounded append-time policy index before loading deliveries."""
        with self.reading() as source, closing(source._source_sequences(
            *DirectReplySource.query(lookup, sender_lookups), after=after,
            descending=descending, sequences=sequences, limit=limit,
        )) as positions:
            return tuple(source.delivery(seq) for seq in positions)

    def unanswered_references(
        self, sequences: tuple[int, ...], responders: frozenset[str],
    ) -> tuple[MessageReference, ...]:
        with self.observing() as db:
            return AnsweredSource.unanswered_references(db, sequences, responders)

    def unanswered_page(
        self, scope: RepliesDisplayScope, *,
        since: float, before: float, upper: int, limit: int = 100,
        kinds: tuple[type[MessageKind], ...] = (),
    ) -> tuple[BusRecord, ...]:
        with self.observing() as db:
            return AnsweredSource.unanswered(
                db, scope, since, before, upper, limit, kinds,
            )

    def latest_answer(
        self, scope: RepliesDisplayScope, *, since: float,
    ) -> tuple[BusRecord, ...]:
        with self.observing() as db:
            return AnsweredSource.latest_answer(db, scope, since)

    def answer_timestamp(
        self, scope: RepliesDisplayScope, *, since: float,
    ) -> float:
        with self.observing() as db:
            return AnsweredSource.answer_timestamp(db, scope, since)

    def replies(self, references: tuple[MessageReference, ...]) -> tuple[BusRecord, ...]:
        with self.observing() as db:
            sequences = {row.seq for original in references
                         for row in AnsweredSource.select(db, original=original.seq)}
            headers = (BusRow.require(db, seq=seq) for seq in sorted(sequences))
            return tuple(header.kind.read_from(db, header.seq) for header in headers)

    def changed_names(self, after: int) -> frozenset[str]:
        with self.observing() as db:
            return frozenset(FieldCodec.decode(str, row[0]) for row in db.execute(
                f'SELECT sender FROM "{Addressed.table.declared_name}" WHERE seq>? UNION '
                f'SELECT target FROM "{Addressed.table.declared_name}" WHERE seq>?', (after, after),
            ))

    def display_unread(
        self, selection: DisplayUnreadSelection, *, through: int, after: int = -1,
    ) -> CountedDisplayUnread:
        """Count one explicit sequence interval with the captured cursor predicate."""
        counts = {}
        with self.observing() as db:
            for scope in selection.scopes:
                bindings = ReadCursor.sql_bindings(selection.registry, {
                    "viewer": selection.seen.counted(scope, selection.registry),
                })
                predicate, parameters = scope.index_predicate
                counts[scope.channel] = FieldCodec.decode(int, db.execute(
                    f'WITH {ReadCursor.sql("?")}, requests AS ('
                    f'SELECT \'viewer\' AS actor,* FROM "{Addressed.table.declared_name}" '
                    f'JOIN "{Message.table.declared_name}" USING(seq) WHERE seq > ? AND seq <= ? '
                    f'AND ({predicate}) AND sender NOT IN (SELECT value FROM json_each(?))), '
                    f'{ReadCursor.joined_sql} SELECT count(*) FROM cursors WHERE {ReadCursor.unread_sql}',
                    (*bindings.values(), max(after, selection.seen.lower_bound(scope, selection.registry)),
                     through, *parameters, json.dumps(selection.viewer_names)),
                ).fetchone()[0])
        return CountedDisplayUnread(through, selection, counts)

    def delivered_unanswered(
        self, recipient: RecordedThreadIncarnation, scope: MessageDisplayScope,
        seen: ReadCursor, snapshot: RegistryProvenance,
    ) -> tuple[MessageReference, ...]:
        """Unanswered originals in this recipient's recorded delivered cursor."""
        bindings = ReadCursor.sql_bindings(snapshot, {"viewer": seen.counted(scope, snapshot)})
        predicate, parameters = scope.index_predicate
        received, recipients = ReceivedSource.query(recipient.lookup)
        with self.observing() as db:
            rows = db.execute(
                f'WITH {ReadCursor.sql("?")}, requests AS ('
                f'SELECT \'viewer\' AS actor,* FROM "{Addressed.table.declared_name}" '
                f'JOIN "{Message.table.declared_name}" USING(seq) '
                f'WHERE seq > 0 AND ({predicate}) AND seq IN ({received})), '
                f'{ReadCursor.joined_sql} '
                f'SELECT DISTINCT seq FROM cursors WHERE {ReadCursor.read_sql}',
                (*bindings.values(), *parameters, *recipients),
            )
            sequences = tuple(FieldCodec.decode(int, row[0]) for row in rows)
            return self.unanswered_references(sequences, snapshot.names_for(recipient.name))

    def selected_sequences(
        self, predicate: str, parameters: tuple[object, ...], through: int,
    ) -> list[AddressedRow]:
        """An explicit bulk read mark projects positions, not message bodies."""
        with self.observing() as db:
            return AddressedRow.matching(
                db, (f'{{seq}} IN (SELECT seq FROM "{Addressed.table.declared_name}" '
                     f'JOIN "{Message.table.declared_name}" USING(seq) '
                     f'WHERE ({predicate}) AND seq <= ?)'),
                (*parameters, through),
            )


class Bus(BusReader, ReconciledFile, WalStore, CarriedWriter, BusFile, Watched):
    """One SQLite transaction owns the sequence and all of a record's fields."""

    filename = "bus.sqlite3"

    def __init__(self, path: Path):
        super().__init__(path)

    def unreceived(self, lookup: ThreadLookup,
                   references: Iterable[MessageReference]) -> tuple[MessageReference, ...]:
        """The references outside this participant's frozen delivery audiences."""
        with self.observing() as db:
            return tuple(reference for reference in references if not ReceivedSource.exists(
                db, where="{seq} = ?", parameters=(reference.seq,), lookup=lookup))

    def received_after(self, lookup: ThreadLookup, after: int) -> bool:
        """Whether this recipient has a newer candidate; read one indexed metadata row."""
        with self.observing() as db:
            return ReceivedSource.exists(db, where="{seq} > ?", parameters=(after,), lookup=lookup)

    @classmethod
    def install(cls, path: Path, *, history: Iterable[BusRecord] = ()) -> Self:
        """Install declared schemas and commit optional copied history atomically.

        Original sequences and frozen deliveries survive; resource claims do not.
        Existing-file migrations still belong to declared carries and their backups.
        """
        bus = cls(path)
        with bus.writer_connection() as db:
            BusSchema.install_schema(db, cls.schema_change(path))
            with bus.write_transaction(db):
                BusSchema.require_installed(db)
                for message in history:
                    message.history_record.append_to(db, message.seq)
        return bus

    @classmethod
    def table_schema(cls) -> type[BusSchema]:
        return BusSchema

    @contextmanager
    def observing(
        self, contention: StoreLockContention = UNBOUNDED_CONTENTION,
    ) -> Iterator[sqlite3.Connection]:
        """Borrow one read-only snapshot, including for an archived bus. Root adoption
        creates the bus, so an absent file is a deleted bus and fails to open."""
        with self.attached(
                self.path, lambda: SQLiteFile.observing(self.path), contention) as db:
            yield db

    def backup(self, destination: Path) -> None:
        """Copy a consistent SQLite snapshot, never replace an existing destination."""
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.touch(mode=0o600, exist_ok=False)
        try:
            with self.observing() as source, closing(
                self.writer(destination, isolation_level=None)
            ) as target:
                source.backup(target)
        except BaseException:
            destination.unlink()
            raise

    def _publish(self, operation: Callable[[sqlite3.Connection], int]) -> BusRecord:
        """One commit and notification lifetime for inserts and conditional claims."""
        with ExitStack() as writes:
            db = writes.enter_context(self.writing())
            header = BusRow.require(db, seq=operation(db))
            committed = header.kind.read_from(db, header.seq)
            try:
                writes.close()
            except StoreCommittedError as error:
                raise CommittedMessageError(committed) from error
            return committed

    def _insert(self, db: sqlite3.Connection, build: RecordBuilder) -> int:
        seq = BusRow.next_seq(db)
        record = build(seq, db)
        FieldCodec.require_fields(record)
        return record.append_to(db, seq)

    def append(self, build: RecordBuilder) -> BusRecord:
        return self._publish(lambda db: self._insert(db, build))

    def resource_claim(self, db: sqlite3.Connection, resource: str) -> ResourceClaim:
        return HeldResources.owner_for(db, resource)

    def prepare_claim[Change: ClaimTransition](
        self, db: sqlite3.Connection, transition: Change, registry: RegistrySnapshot,
    ) -> Change:
        previous = HeldResources.all_claims(db)
        projected = transition.apply(previous, registry)
        transition.require_new_operation(lambda operation: ClaimOperations.used(db, operation))
        return replace(transition, expired=tuple(
            resource for resource in previous
            if resource not in projected and resource not in transition.resources
        ))

    def acquire_resource_claim(
        self, resource: str, claim: RecordedScope, registry: RegistrySnapshot, build: RecordBuilder,
    ) -> ClaimOwner:
        def acquire(db: sqlite3.Connection) -> int:
            def publish() -> ClaimOwner:
                seq = self._insert(db, build)
                header = BusRow.require(db, seq=seq)
                return header.kind.read_from(db, seq).require_claim_transition().claimed(resource)

            return self.resource_claim(db, resource).acquire(claim, registry, publish).seq

        return self._publish(acquire).require_claim_transition().claimed(resource)


@dataclass(frozen=True, slots=True)
class BusSnapshot(PresentPath, BusReader):
    """One held connection owns both its source cut and captured file identity."""

    path: Path
    _connection: sqlite3.Connection

    @classmethod
    def from_connection(cls, connection: sqlite3.Connection) -> Self:
        database = SQLiteDatabase.read(connection.execute(
            "SELECT * FROM pragma_database_list WHERE name='main'",
        ))[0]
        path = Path(database.file)
        return cls(identity=FileIdentity.from_stat(path.stat()), path=path, _connection=connection)

    @contextmanager
    def observing(
        self, contention: StoreLockContention = UNBOUNDED_CONTENTION,
    ) -> Iterator[sqlite3.Connection]:
        """The held snapshot is already attached, so it takes no lock."""
        yield self._connection

    @contextmanager
    def reading(self) -> Iterator[BusSnapshot]:
        yield self
