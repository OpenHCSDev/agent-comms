"""Activity maxima derived in the transaction that appends their original message."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated

from .bus_tables import BusProjectionTables
from .typed_table import Column, TypedRow, TypedTable

if TYPE_CHECKING:
    from .channels import Channel
    from .messages import Message


@dataclass(frozen=True, slots=True)
class ChannelActivity(TypedRow):
    last_message: float = 0
    last_user_input: float = 0

    def observe(self, message: Message) -> ChannelActivity:
        return ChannelActivity(
            max(self.last_message, message.timestamp),
            message.sender_role.last_user_input(self.last_user_input, message.timestamp),
        )

    @classmethod
    def from_message(cls, message: Message, **key):
        return cls(last_message=message.timestamp,
                   last_user_input=message.sender_role.last_user_input(0.0, message.timestamp),
                   **key)

    @classmethod
    def combine(cls, clocks) -> ChannelActivity:
        clocks = tuple(clocks)
        return cls(max((item.last_message for item in clocks), default=0),
                   max((item.last_user_input for item in clocks), default=0))

    @classmethod
    def for_views(cls, channels: Mapping[str, Channel],
                  by_target: Mapping[str, ChannelActivity]) -> Mapping[str, ChannelActivity]:
        return {name: cls.combine(channel.history_scope().target_values(by_target))
                for name, channel in channels.items()}


@dataclass(frozen=True)
class ActivityClocks:
    channels: dict[str, ChannelActivity] = field(default_factory=dict)
    sent: dict[str, float] = field(default_factory=dict)


class ActivityMaximum(TypedTable, BusProjectionTables, declared_name=None):
    """The declared non-key values are maxima, not independently writable clocks."""

    def insert(self, db):
        statement, values = self._insertion()
        keys = tuple(item.name for item in self._fields() if item.column.primary_key)
        updates = tuple(item.wire_name for item in self._writable_fields()
                        if not item.column.primary_key)
        statement += f" ON CONFLICT ({self._column_list(keys)}) DO UPDATE SET "
        statement += ", ".join(f'"{name}"=max("{name}",excluded."{name}")' for name in updates)
        return db.execute(statement, values)


    @classmethod
    def aggregate(cls, db, predicate: str, parameters: tuple) -> ChannelActivity:
        result, = ChannelActivity.read(db.execute(
            f'SELECT coalesce(max(last_message),0.0) AS last_message, '
            f'coalesce(max(last_user_input),0.0) AS last_user_input '
            f'FROM "{cls.declared_name}" WHERE {predicate}', parameters))
        return result


@dataclass(frozen=True)
class BusActivityIndex(ChannelActivity, ActivityMaximum):
    target: Annotated[str, Column(primary_key=True)] = field(kw_only=True)

    @classmethod
    def clocks(cls, db) -> ActivityClocks:
        return ActivityClocks(
            {row.target: ChannelActivity(row.last_message, row.last_user_input)
             for row in cls.select(db)},
            {row.sender: row.last_sent for row in SentActivity.select(db)},
        )


@dataclass(frozen=True)
class SentActivity(ActivityMaximum):
    sender: Annotated[str, Column(primary_key=True)]
    last_sent: float
