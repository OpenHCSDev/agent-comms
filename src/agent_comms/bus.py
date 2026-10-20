"""An append-only, capability-declared SQLite bus; no existing readers are wired here."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, fields, make_dataclass
from pathlib import Path
from typing import ClassVar, get_type_hints

from .coordination_database import CoordinationStore
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .typed_table import (
    AppendOnlyTables, Column, DurableTables, SQLiteTableName, StoreWriter, TypedTable,
)


class BusTables(DurableTables, AppendOnlyTables):
    """Bus history cannot be rebuilt from another source."""


@dataclass(frozen=True)
class BusRow(TypedTable, BusTables):
    seq: int = field(metadata={"sql": Column(primary_key=True, auto_increment=True)})
    kind: str
    at: float


class BusCapability(BusTables):
    """A field declaration derives its frozen value and its one SQLite table."""

    table: ClassVar[type[TypedTable]]

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        own = vars(cls).get("__annotations__", {})
        if not own:
            return  # Record members compose capabilities; they declare no new fields.
        dataclass(cls, frozen=True)
        hints = get_type_hints(cls, include_extras=True)
        cls.table = make_dataclass(
            BusTables.__name__.removesuffix("Tables") + cls.__name__ + "Row",
            [("seq", int, field(metadata={"sql": Column(
                primary_key=True, references=(BusRow, "seq"))})),
             *((item.name, hints[item.name], item) for item in fields(cls) if item.name in own)],
            bases=(TypedTable, BusTables),
            namespace={"__module__": cls.__module__},
            frozen=True,
        )


class Addressed(BusCapability):
    sender: str
    target: str = field(metadata={"sql": Column(index=True)})


class KeyedPublication(BusCapability):
    publication_key: str = field(metadata={"sql": Column(unique=True)})


class Claimed(BusCapability):
    claim_owner: str


class MessageText(BusCapability):
    body: str


class BusRecord(DeclaredFamily, affix="Record"):
    """Kinds select their fields and tables by composing capabilities."""

    @classmethod
    def capabilities(cls) -> tuple[type[BusCapability], ...]:
        return tuple(base for base in cls.__mro__
                     if issubclass(base, BusCapability) and "table" in vars(base))


@dataclass(frozen=True)
class PublicMessage(BusRecord, Addressed, KeyedPublication, MessageText):
    pass


@dataclass(frozen=True)
class PrivateDelivery(BusRecord, Addressed, KeyedPublication, MessageText, Claimed):
    pass


class Bus(StoreWriter):
    """One SQLite transaction owns the sequence and all of a record's fields."""

    def __init__(self, path: Path):
        super().__init__(path)
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
            prior = KeyedPublication.table.select(db, publication_key=record.publication_key)
            if prior:
                return prior[0].seq
            cursor = BusRow.insert_values(db, kind=type(record).declared_name, at=time.time())
            seq = FieldCodec.decode(int, cursor.lastrowid)
            for capability in type(record).capabilities():
                capability.table(seq=seq, **{
                    item.name: getattr(record, item.name) for item in fields(capability)
                }).insert(db)
            return seq

    @staticmethod
    def _record(db: sqlite3.Connection, header: BusRow) -> BusRecord:
        member = FieldCodec.decode(type[BusRecord], header.kind)
        values = {BusRecord.family_discriminator: header.kind}
        for capability in member.capabilities():
            rows = capability.table.select(db, seq=header.seq)
            if not rows:
                raise RelationViolationError(
                    f"Bus row {header.seq} lacks {capability.__name__}")
            values.update((item.name, getattr(rows[0], item.name)) for item in fields(capability))
        return FieldCodec.decode(BusRecord, values)

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
