"""Bus field declarations, independent of Message and publication lifetimes."""

from __future__ import annotations

import agent_comms
from dataclasses import dataclass, field, fields, make_dataclass
from functools import cache
from typing import TYPE_CHECKING, ClassVar, get_type_hints
from weakref import WeakSet

from agent_comms.typed_table import AppendOnlyTables, Column, DurableTables, TypedTable

if TYPE_CHECKING:
    from agent_comms.delivery_policy import KeyedResponseAcknowledgement


class BusTables(DurableTables, AppendOnlyTables):
    """Bus history cannot be rebuilt from another source."""


@dataclass(frozen=True)
class BusRow(TypedTable, BusTables):
    seq: int = field(metadata={"sql": Column(primary_key=True, auto_increment=True)})
    kind: str
    at: float


class _CapabilityTable:
    """A class's table is derived once, after its dataclass decorator has run."""

    def __get__(self, instance, owner) -> type[TypedTable]:
        return self.for_class(owner)

    @cache
    def for_class(self, owner) -> type[TypedTable]:
        hints = get_type_hints(owner, include_extras=True)
        own = vars(owner).get("__annotations__", {})
        header_fields = {item.name for item in fields(BusRow)}
        return make_dataclass(
            BusTables.__name__.removesuffix("Tables") + owner.__name__ + "Row",
            [("seq", int, field(metadata={"sql": Column(
                primary_key=True, references=(BusRow, "seq"))})),
             *((item.name, hints[item.name], item) for item in fields(owner)
               if item.name in own and item.name not in header_fields)],
            bases=(TypedTable, BusTables),
            namespace={"__module__": owner.__module__},
            frozen=True,
        )


class BusCapability(BusTables):
    """Dataclasses register here; their declared fields own their shared tables."""

    _declarations: ClassVar[WeakSet[type[BusCapability]]] = WeakSet()
    table: ClassVar[type[TypedTable]] = _CapabilityTable()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        BusCapability._declarations.add(cls)

    @classmethod
    @cache
    def tables(cls) -> tuple[type[TypedTable], ...]:
        header_fields = {item.name for item in fields(BusRow)}
        owners = {
            base for base in cls.__mro__ if base in BusCapability._declarations
            for item in fields(cls)
            if item.name in vars(base).get("__annotations__", {}) and item.name not in header_fields
        }
        return tuple(base.table for base in cls.__mro__ if base in owners)


@dataclass(frozen=True)
class Addressed(BusCapability):
    sender: str = field(metadata={"wire_name": "from", "wire_order": 2})
    target: str = field(metadata={"wire_name": "to", "wire_order": 3, "sql": Column(index=True)})


@dataclass(frozen=True)
class KeyedPublication(BusCapability):
    acknowledgement: agent_comms.delivery_policy.KeyedResponseAcknowledgement = field(
        metadata={"sql": Column(unique=True)})
