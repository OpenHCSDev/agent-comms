"""Bus field declarations, independent of Message and publication lifetimes."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import Field, dataclass, fields, make_dataclass, replace
from functools import cache
from inspect import get_annotations
from typing import Annotated, get_type_hints

from agent_comms import messages
from agent_comms.field_codec import FieldCodec, WireName, WireOrder
from agent_comms.response_acknowledgement import KeyedResponseAcknowledgement
from agent_comms.typed_table import AppendOnlyTables, Column, DerivedTables, DurableTables, Index, SharedFileTables, TypedTable
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
        """One row per capability table, each cut from this record's own encoding by the
        table's declared keys. The table reuses the record's fields, so a key the encoding
        omits for its default decodes to the same default."""
        encoded = FieldCodec.encode_fields(self)
        return tuple(
            FieldCodec.decode_record(table, {
                key: encoded[key] for _, key in FieldCodec.record_fields(table) if key in encoded})
            for table in self.tables())

    def projections(self):
        return iter(())

    def append_to(self, db: sqlite3.Connection, seq: int) -> int:
        """The caller's transaction owns the sequence, including preserved history."""
        BusRow.insert_values(db, seq=seq, kind=type(self), at=time.time())
        stored = replace(self, seq=seq)
        for row in stored.rows():
            row.insert(db)
        for row in stored.projections():
            row.insert(db)
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
