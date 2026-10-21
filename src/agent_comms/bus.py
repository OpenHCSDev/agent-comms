"""An append-only, capability-declared SQLite bus; no existing readers are wired here."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent_comms.bus_publication import Delivered
from agent_comms.bus_tables import (
    Addressed,
    BusRecord,
    BusRow,
    BusTables,
    KeyedPublication,
)
from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.locked_store import DurableFile, RegistryFile
from agent_comms.maintenance_barrier import MaintenanceBarrier
from agent_comms.messages import ClaimMessage, Message
from agent_comms.typed_table import SQLiteTableName, StoreNotificationError, StoreWriter


@dataclass(frozen=True)
class PublicRecord(BusRecord, Message):
    pass


@dataclass(frozen=True)
class ClaimRecord(PublicRecord, ClaimMessage):
    pass


@dataclass(frozen=True)
class PrivateRecord(BusRecord, Message, Delivered):
    pass


@dataclass(frozen=True)
class KeyedPrivateRecord(PrivateRecord, KeyedPublication):
    pass


class MessageNotificationError(RuntimeError):
    """The message committed; only notifying its readers failed."""

    def __init__(self, record: BusRecord) -> None:
        self.record = record
        super().__init__(f"Message {record.seq}/{record.message_id} was sent, but notification failed.")


class Bus(StoreWriter, DurableFile):
    """One SQLite transaction owns the sequence and all of a record's fields."""

    filename = "bus.sqlite3"

    def __init__(self, path: Path):
        super().__init__(path)
        for member in BusRecord.members_with(BusRecord):
            member.tables()
        with self.writing() as db:
            if not SQLiteTableName.stored(db):
                BusTables.create_schema(db)
            BusTables.require_schema(db)

    @contextmanager
    def writing(self):
        with closing(CoordinationStore.writer(self.path, isolation_level=None)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            with self.transaction(db):
                yield db

    def append(self, build: Callable[[int], BusRecord]) -> BusRecord:
        with ExitStack() as writes:
            db = writes.enter_context(self.writing())
            MaintenanceBarrier(RegistryFile.path_in(self.path.parent)).assert_open_unlocked()
            seq = BusRow.next_seq(db)
            record = build(seq)
            committed = self._record(db, BusRow.require(db, seq=record.append_to(db, seq)))
            try:
                writes.close()
            except StoreNotificationError as error:
                raise MessageNotificationError(committed) from error
            return committed

    @staticmethod
    def _record(db: sqlite3.Connection, header: BusRow) -> BusRecord:
        member = header.kind
        values = {}
        for table in member.tables():
            rows = table.select(db, seq=header.seq)
            if not rows:
                raise RelationViolationError(
                    f"Bus row {header.seq} lacks {table.declared_name}")
            values.update(FieldCodec.encode_fields(rows[0]))
        return FieldCodec.decode_record(member, values)

    def lookup(self, seq: int) -> BusRecord:
        with CoordinationStore.observing(self.path) as db:
            headers = BusRow.select(db, seq=seq)
            if not headers:
                raise RelationViolationError(f"Unknown bus sequence: {seq}")
            return self._record(db, headers[0])

    def page(self, before: int, limit: int) -> tuple[BusRecord, ...]:
        with CoordinationStore.observing(self.path) as db:
            return tuple(self._record(db, row) for row in BusRow.select(
                db, where="{seq} < ?", parameters=(before,),
                order_by=("seq",), descending=True, limit=limit))

    def inbox(self, target: str, after: int, limit: int) -> tuple[BusRecord, ...]:
        with CoordinationStore.observing(self.path) as db:
            addressed = Addressed.table.select(
                db, where="{seq} > ?", parameters=(after,), order_by=("seq",), limit=limit,
                target=target)
            return tuple(self._record(db, header) for row in addressed
                         for header in BusRow.select(db, seq=row.seq))
