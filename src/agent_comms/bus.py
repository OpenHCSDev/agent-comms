"""Append-only database history and capability-declared record queries."""

from __future__ import annotations

import sqlite3
import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Self

from agent_comms.bus_publication import Delivered, KeyedPublication
from agent_comms.bus_activity_index import BusActivityIndex
from agent_comms.bus_display_index import BusDisplayIndex
from agent_comms.bus_route_counts import RouteCounts
from agent_comms.bus_tables import (
    Addressed,
    BusRow,
    BusTables,
    BusProjectionTables,
    ClaimOperations,
    HeldResources,
    MessageIdentitySource,
    SentSource,
    ReceivedSource,
    AnsweredSource,
)
from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import CommittedMessageError, RelationViolationError, UnclaimedError
from agent_comms.envelope_claim_transitions import (
    ClaimOwner, ClaimProjection, ClaimTransition, RecordedScope, ResourceClaim,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.locked_store import DurableFile, RegistryFile, StoreCommittedError
from agent_comms.maintenance_barrier import MaintenanceBarrier
from agent_comms.messages import BusRecord, Claimed, ClaimMessage, Message
from agent_comms.message_reference import MessageReference
from agent_comms.typed_table import SQLiteTableName, WalStore
from agent_comms.watched import Watched

if TYPE_CHECKING:
    from agent_comms.registry_document import RegistrySnapshot


class PublicRecord(BusRecord):
    pass


class ClaimRecord(PublicRecord, ClaimMessage):
    pass


class PrivateRecord(Delivered):
    pass


class KeyedPrivateRecord(PrivateRecord, KeyedPublication):
    pass


type RecordBuilder = Callable[[int, sqlite3.Connection], BusRecord]


class BusReader(RouteCounts, ABC):
    """One query implementation for individual reads and an admitted read snapshot."""

    @abstractmethod
    def observing(self) -> AbstractContextManager[sqlite3.Connection]: ...

    @contextmanager
    def reading(self) -> Iterator[BusReader]:
        """Keep a batch of queries on one admitted SQLite snapshot."""
        with self.observing() as db:
            yield BusSnapshot(db)

    def revision(self) -> int:
        with self.observing() as db:
            return BusRow.last_seq(db)

    def activity_clocks(self):
        with self.observing() as db:
            return BusActivityIndex.clocks(db)

    def target_activity(self, predicate: str, parameters: tuple):
        with self.observing() as db:
            return BusActivityIndex.aggregate(db, predicate, parameters)

    def participant_activity(self, names: frozenset[str]):
        marks = ','.join('?' for _ in names)
        with self.observing() as db:
            return BusDisplayIndex.aggregate(db, f'participant IN ({marks})', tuple(names))

    def claim_projection(self) -> ClaimProjection:
        with self.observing() as db:
            return HeldResources.all_claims(db)

    def resource_owner(self, resource: str) -> ResourceClaim:
        with self.observing() as db:
            return HeldResources.owner_for(db, resource)

    def claim_operation_used(self, operation_id: str) -> bool:
        with self.observing() as db:
            return ClaimOperations.used(db, operation_id)

    def claim(self, seq: int) -> ClaimTransition:
        """Read attestation from the row's capability, never a retained-history floor."""
        with self.observing() as db:
            rows = Claimed.table.select(db, seq=seq)
            if not rows:
                raise UnclaimedError(seq)
            return rows[0].claim_transition

    def lookup(self, seq: int) -> BusRecord:
        with self.observing() as db:
            headers = BusRow.select(db, seq=seq)
            if not headers:
                raise RelationViolationError(f"Unknown bus sequence: {seq}")
            return headers[0].kind.read_from(db, headers[0].seq)

    def lookup_message(self, message_id: str) -> BusRecord:
        """Resolve one content identity; absent or ambiguous identities are refused."""
        with self.observing() as db:
            identity = MessageIdentitySource.require(db, message_id=message_id)
            header = BusRow.require(db, seq=identity.seq)
            return header.kind.read_from(db, header.seq)

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
            addressed = Addressed.table.select(
                db, where="{seq} > ?", parameters=(after,), order_by=("seq",), limit=limit,
                target=target)
            return tuple(header.kind.read_from(db, header.seq) for row in addressed
                         for header in BusRow.select(db, seq=row.seq))

    def scoped_page(
        self, predicate: str, parameters: tuple, *, lower: int = 0,
        upper: int | None = None, descending: bool = False, limit: int = 100, kinds=(),
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

    def latest_conversations(self, predicate: str, parameters: tuple, through: int):
        """One prefix per addressed conversation, without materializing its history."""
        with self.observing() as db:
            return Addressed.table.select(
                db, where=(f'{{seq}} IN (SELECT MAX(seq) FROM '
                           f'"{Addressed.table.declared_name}" '
                           f'JOIN "{Message.table.declared_name}" USING(seq) '
                           f'WHERE ({predicate}) AND seq <= ? GROUP BY sender, target)'),
                parameters=(*parameters, through),
            )

    def messages_for_references(self, references):
        with self.observing() as db:
            headers = (BusRow.require(db, seq=ref.seq) for ref in references)
            records = tuple(header.kind.read_from(db, header.seq) for header in headers)
        for reference, record in zip(references, records, strict=True):
            if record.reference != reference:
                raise RelationViolationError("Message reference does not match its bus record")
        return records

    def deliveries_for_references(self, references):
        return tuple(delivery for record in self.messages_for_references(references)
                     for delivery in record.deliveries())

    def _source_page(self, query, parameters, *, after=0, before=None, limit=100,
                     descending=True, sequences=()):
        with self.observing() as db:
            before = BusRow.last_seq(db) + 1 if before is None else before
            selected = ""
            if sequences:
                selected = " AND seq IN (" + ",".join("?" for _ in sequences) + ")"
            order = "DESC" if descending else "ASC"
            rows = db.execute(
                f"SELECT seq FROM ({query}) WHERE seq > ? AND seq < ?{selected} "
                f"ORDER BY seq {order} LIMIT ?",
                (*parameters, after, before, *sequences, limit),
            )
            headers = (BusRow.require(db, seq=FieldCodec.decode(int, row[0])) for row in rows)
            return tuple(header.kind.read_from(db, header.seq) for header in headers)

    def conversation(self, lookup: str, *, after=0, before=None, limit=100,
                     descending=True, sequences=()):
        return self._source_page(
            f'SELECT seq FROM "{SentSource.declared_name}" WHERE lookup=? UNION '
            f'SELECT seq FROM "{ReceivedSource.declared_name}" WHERE lookup=?',
            (lookup, lookup), after=after, before=before, limit=limit,
            descending=descending, sequences=sequences,
        )

    def sent(self, lookup: str, *, after=0, before=None, limit=100, descending=True):
        return self._source_page(
            f'SELECT seq FROM "{SentSource.declared_name}" WHERE lookup=?', (lookup,),
            after=after, before=before, limit=limit, descending=descending,
        )

    def received(self, lookup: str, *, after=0, before=None, limit=100,
                 sender_lookups=(), descending=True, source: type[ReceivedSource] = ReceivedSource,
                 sequences=()):
        sender = ""
        if sender_lookups:
            marks = ",".join("?" for _ in sender_lookups)
            sender = (f' AND seq IN (SELECT seq FROM "{SentSource.declared_name}" '
                      f'WHERE lookup IN ({marks}))')
        return self._source_page(
            f'SELECT seq FROM "{source.declared_name}" WHERE lookup=?{sender}',
            (lookup, *sender_lookups), after=after, before=before, limit=limit,
            descending=descending, sequences=sequences,
        )

    def unanswered_page(
        self, requesters: frozenset[str], responders: frozenset[str], *,
        since: float, before: float, upper: int, limit: int = 100,
    ) -> tuple[BusRecord, ...]:
        with self.observing() as db:
            return AnsweredSource.unanswered(db, requesters, responders, since, before, upper, limit)

    def replies(self, references: tuple[MessageReference, ...]):
        with self.observing() as db:
            sequences = {row.seq for original in references
                         for row in AnsweredSource.select(db, original=original)}
            headers = (BusRow.require(db, seq=seq) for seq in sorted(sequences))
            return tuple(header.kind.read_from(db, header.seq) for header in headers)

    def changed_names(self, after: int) -> frozenset[str]:
        with self.observing() as db:
            return frozenset(FieldCodec.decode(str, row[0]) for row in db.execute(
                f'SELECT sender FROM "{Addressed.table.declared_name}" WHERE seq>? UNION '
                f'SELECT target FROM "{Addressed.table.declared_name}" WHERE seq>?', (after, after),
            ))

    def display_unread(self, scopes, viewer_names, seen, snapshot):
        from .read_basis import ReadCursor

        counts = {}
        with self.observing() as db:
            for scope in scopes:
                bindings = ReadCursor.sql_bindings(snapshot, {"viewer": seen.counted(scope, snapshot)})
                predicate, parameters = scope.index_predicate
                counts[scope.channel] = FieldCodec.decode(int, db.execute(
                    f'WITH {ReadCursor.sql("?")}, requests AS ('
                    f'SELECT \'viewer\' AS actor,* FROM "{Addressed.table.declared_name}" '
                    f'JOIN "{Message.table.declared_name}" USING(seq) WHERE seq > ? '
                    f'AND ({predicate}) AND sender NOT IN (SELECT value FROM json_each(?))), '
                    f'{ReadCursor.joined_sql} SELECT count(*) FROM cursors WHERE {ReadCursor.unread_sql}',
                    (*bindings.values(), seen.lower_bound(scope, snapshot), *parameters,
                     json.dumps(tuple(viewer_names))),
                ).fetchone()[0])
        return counts

    def selected_sequences(self, predicate, parameters, through):
        """An explicit bulk read mark projects positions, not message bodies."""
        with self.observing() as db:
            return Addressed.table.select(
                db, where=(f'{{seq}} IN (SELECT seq FROM "{Addressed.table.declared_name}" '
                           f'JOIN "{Message.table.declared_name}" USING(seq) '
                           f'WHERE ({predicate}) AND seq <= ?)'),
                parameters=(*parameters, through),
            )


class Bus(BusReader, WalStore, DurableFile, Watched):
    """One SQLite transaction owns the sequence and all of a record's fields."""

    filename = "bus.sqlite3"

    def __init__(self, path: Path):
        super().__init__(path)

    @classmethod
    def install(cls, path: Path) -> Self:
        """Create a fresh bus during installation; never migrate an existing file.

        Changes to existing history or its projections belong to a declared carry,
        with its backup and rewritten-file accounting, not ordinary attachment.
        """
        bus = cls(path)
        with bus.writer_connection() as db:
            with bus.write_transaction(db):
                if not SQLiteTableName.stored(db):
                    BusTables.create_schema(db)
                    BusProjectionTables.create_schema(db)
                cls.require_schema(db)
        return bus

    @classmethod
    def require_schema(cls, db: sqlite3.Connection) -> None:
        """Installation validates both families sharing this physical file."""
        BusTables.require_schema(db)
        BusProjectionTables.require_schema(db)

    @contextmanager
    def observing(self) -> Iterator[sqlite3.Connection]:
        """Borrow one existing read-only snapshot, including for an archived bus."""
        with self._attached(lambda: CoordinationStore.observing(self.path)) as db:
            yield db

    def backup(self, destination: Path) -> None:
        """Copy a consistent SQLite snapshot, never replace an existing destination."""
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.touch(mode=0o600, exist_ok=False)
        try:
            with self.observing() as source, closing(
                CoordinationStore.writer(destination, isolation_level=None)
            ) as target:
                source.backup(target)
        except BaseException:
            destination.unlink()
            raise

    def _publish(self, operation: Callable[[sqlite3.Connection], int]) -> BusRecord:
        """One commit and notification lifetime for inserts and conditional claims."""
        with ExitStack() as writes:
            db = writes.enter_context(self.writing())
            MaintenanceBarrier(RegistryFile.path_in(self.path.parent)).assert_open_unlocked()
            header = BusRow.require(db, seq=operation(db))
            committed = header.kind.read_from(db, header.seq)
            try:
                writes.close()
            except StoreCommittedError as error:
                raise CommittedMessageError(committed) from error
            return committed

    def _insert(self, db: sqlite3.Connection, build: RecordBuilder) -> int:
        seq = BusRow.next_seq(db)
        return build(seq, db).append_to(db, seq)

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
                return Claimed.table.require(db, seq=seq).claim_transition.claimed(resource)

            return self.resource_claim(db, resource).acquire(claim, registry, publish).seq

        return self._publish(acquire).require_claim_transition().claimed(resource)


class BusSnapshot(BusReader):
    """The surrounding reading scope owns this already validated connection."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    @contextmanager
    def observing(self) -> Iterator[sqlite3.Connection]:
        yield self._connection
