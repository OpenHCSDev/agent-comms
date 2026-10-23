"""Activity maxima derived in the transaction that appends their original message."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated

from . import activity_clocks
from .messages import BusProjectionTables
from .typed_table import Column

if TYPE_CHECKING:
    pass


class ActivityMaximum(BusProjectionTables, declared_name=None):
    """The declared non-key values are maxima, not independently writable clocks."""

    def insertion(self) -> tuple[str, tuple]:
        statement, values = self._insertion()
        keys = tuple(item.name for item in self._fields if item.column.primary_key)
        updates = tuple(item.wire_name for item in self._writable_fields()
                        if not item.column.primary_key)
        statement += f" ON CONFLICT ({self._column_list(keys)}) DO UPDATE SET "
        statement += ", ".join(f'"{name}"=max("{name}",excluded."{name}")' for name in updates)
        return statement, values


    @classmethod
    def aggregate(cls, db: sqlite3.Connection, predicate: str,
                  parameters: tuple[object, ...]) -> activity_clocks.ChannelActivity:
        result, = activity_clocks.ChannelActivity.read(db.execute(
            f'SELECT coalesce(max(last_message),0.0) AS last_message, '
            f'coalesce(max(last_user_input),0.0) AS last_user_input '
            f'FROM "{cls.declared_name}" WHERE {predicate}', parameters))
        return result


@dataclass(frozen=True)
class BusActivityIndex(activity_clocks.ChannelActivity, ActivityMaximum):
    target: Annotated[str, Column(primary_key=True)] = field(kw_only=True)

    @classmethod
    def clocks(cls, db: sqlite3.Connection) -> activity_clocks.ActivityClocks:
        return activity_clocks.ActivityClocks(
            {row.target: activity_clocks.ChannelActivity(row.last_message, row.last_user_input)
             for row in cls.select(db)},
            {row.sender: row.last_sent for row in SentActivity.select(db)},
        )


@dataclass(frozen=True)
class SentActivity(ActivityMaximum):
    sender: Annotated[str, Column(primary_key=True)]
    last_sent: float
