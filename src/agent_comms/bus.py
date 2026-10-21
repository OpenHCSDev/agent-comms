"""An append-only, capability-declared SQLite bus; no existing readers are wired here."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent_comms.bus_publication import Delivered
from agent_comms.bus_tables import (
    Addressed, BusCapability, BusRecord, BusRow, BusTables, KeyedPublication,
)
from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message
from agent_comms.typed_table import SQLiteTableName, StoreWriter


@dataclass(frozen=True)
class PublicRecord(BusRecord, Message):
    pass


@dataclass(frozen=True)
class KeyedPublicRecord(PublicRecord, KeyedPublication):
    pass


@dataclass(frozen=True)
class PrivateRecord(BusRecord, Message, Delivered):
    pass


@dataclass(frozen=True)
class KeyedPrivateRecord(PrivateRecord, KeyedPublication):
    pass


class Bus(StoreWriter):
    """One SQLite transaction owns the sequence and all of a record's fields."""

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

    def append(self, record: BusRecord) -> int:
        with self.writing() as db:
            if issubclass(type(record), KeyedPublication):
                prior = KeyedPublication.table.select(db, acknowledgement=record.acknowledgement)
                if prior:
                    return prior[0].seq
            cursor = BusRow.insert_values(db, kind=type(record), at=time.time())
            seq = FieldCodec.decode(int, cursor.lastrowid)
            for table in type(record).tables():
                table(seq=seq, **{
                    item.name: getattr(record, item.name)
                    for item in BusCapability.declared_fields(table)
                }).insert(db)
            return seq

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
        values[FieldCodec.record_key(Message, "seq")] = header.seq
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
            headers = BusRow.read(db.execute(
                f'SELECT b.* FROM "{BusRow.declared_name}" b '
                f'JOIN "{Addressed.table.declared_name}" a ON a.seq=b.seq '
                "WHERE a.target=? AND a.seq>? ORDER BY a.seq LIMIT ?",
                (target, after, limit)))
            return tuple(self._record(db, row) for row in headers)
