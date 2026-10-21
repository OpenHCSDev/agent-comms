"""Bus field declarations, independent of Message and publication lifetimes."""

from __future__ import annotations

from dataclasses import Field, dataclass, field, fields, make_dataclass
from functools import cache
from inspect import get_annotations
from typing import get_type_hints

from agent_comms.declared_family import DeclaredFamily
from agent_comms.response_acknowledgement import KeyedResponseAcknowledgement
from agent_comms.typed_table import AppendOnlyTables, Column, DurableTables, TypedTable


class BusRecord(DeclaredFamily, affix="Record"):
    """The record family also owns the header's stored class identity."""


class BusTables(DurableTables, AppendOnlyTables):
    """Bus history cannot be rebuilt from another source."""


@dataclass(frozen=True)
class BusRow(TypedTable, BusTables):
    seq: int = field(metadata={"sql": Column(primary_key=True, auto_increment=True)})
    kind: type[BusRecord]
    at: float


class _CapabilityTable:
    """A class's table is derived once, after its dataclass decorator has run."""

    def __get__(self, instance, owner) -> type[TypedTable]:
        return self.for_class(owner)

    @cache
    def for_class(self, owner) -> type[TypedTable]:
        hints = get_type_hints(owner, include_extras=True)
        return make_dataclass(
            BusTables.__name__.removesuffix("Tables") + owner.__name__ + "Row",
            [("seq", int, field(metadata={"sql": Column(
                primary_key=True, references=(BusRow, "seq"))})),
             *((item.name, hints[item.name], item) for item in BusCapability.declared_fields(owner))],
            bases=(TypedTable, BusTables),
            namespace={"__module__": owner.__module__},
            frozen=True,
        )


@dataclass(frozen=True)
class BusCapability(BusTables):
    """The hierarchy and each dataclass declaration own their table union."""

    table = _CapabilityTable()

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


@dataclass(frozen=True)
class Addressed(BusCapability):
    sender: str = field(metadata={"wire_name": "from", "wire_order": 2})
    target: str = field(metadata={"wire_name": "to", "wire_order": 3, "sql": Column(index=True)})


@dataclass(frozen=True)
class KeyedPublication(BusCapability):
    acknowledgement: KeyedResponseAcknowledgement = field(metadata={"sql": Column(unique=True)})
