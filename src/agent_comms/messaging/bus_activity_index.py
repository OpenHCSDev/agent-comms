"""Activity maxima derived in the transaction that appends their original message."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Annotated

from agent_comms.messaging import activity_clocks
from agent_comms.messaging.messages import BusProjectionTables
from agent_comms.messaging.projection_source import ProducedRow
from agent_comms.nominal.class_fact import ClassFact
from agent_comms.storage.typed_table import Column


class ActivityMaximum(BusProjectionTables, declared_name=None):
    """The declared non-key values are maxima, not independently writable clocks."""

    @ClassFact
    def writes(cls) -> tuple[type[ProducedRow], ...]:
        from agent_comms.messaging.bus_display_index import BusDisplayIndex

        return (BusActivityIndex, SentActivity, BusDisplayIndex)

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
            cls.channels(cls.select(db)),
            {row.sender: row.last_sent for row in SentActivity.select(db)},
        )

    @classmethod
    def clocks_of(cls, db: sqlite3.Connection, target: str) -> activity_clocks.ActivityClocks:
        """One channel's clocks, from its own row."""
        return activity_clocks.ActivityClocks(cls.channels(cls.select(db, target=target)))

    @staticmethod
    def channels(rows: Iterable[BusActivityIndex]) -> dict[str, activity_clocks.ChannelActivity]:
        return {row.target: activity_clocks.ChannelActivity(row.last_message, row.last_user_input)
                for row in rows}


@dataclass(frozen=True)
class SentActivity(ActivityMaximum):
    sender: Annotated[str, Column(primary_key=True)]
    last_sent: float
