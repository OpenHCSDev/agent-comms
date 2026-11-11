"""Which board rows have reached one reader: delivery evidence, not a copy of Core's board."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.comms import Comms
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.errors import CoordinationBeginUnavailable
from agent_comms.coordination.schema import CoordinatorRow
from agent_comms.coordination.session import CoordinationSession
from agent_comms.registry.thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from agent_comms.storage.locked_store import RegistryFile
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier
from agent_comms.storage.typed_table import Column, WithoutRowid
from agent_comms.task_attachments.team_pings import TeamUpdate
from agent_comms_hooks.board import BoardEntry, SessionBoard


@dataclass(frozen=True)
class ReaderBoardDelivery(CoordinatorRow, WithoutRowid):
    """One board row delivered to one reader birth. Every adapter that prints a reader's rows
    (the bridge's tool-call hook, a verb's reply) delivers through here, so a row reaches its
    reader once however many adapters that reader uses."""

    reader: Annotated[str, Column(primary_key=True)]
    content: Annotated[str, Column(primary_key=True)]
    since: float
    delivered_at: float

    def alarm_due(self, entry: BoardEntry, now: float) -> bool:
        return self.delivered_at < entry.alarm_at <= now

    @staticmethod
    def column(reader: RecordedThreadIncarnation) -> str:
        """The stored form of the reader's birth, the table's key; only SQL sees it."""
        return str(reader.lookup)

    @classmethod
    def due(cls, db: sqlite3.Connection, reader: RecordedThreadIncarnation,
            entries: tuple[BoardEntry, ...], now: float) -> tuple[BoardEntry, ...]:
        """The entries this reader has not been shown since they last changed or alarmed."""
        return tuple(
            entry for entry in entries
            if all(delivered.alarm_due(entry, now)
                   for delivered in cls.select(db, reader=cls.column(reader),
                                               content=entry.content, since=entry.since)))

    @classmethod
    def forgotten(cls, db: sqlite3.Connection, reader: RecordedThreadIncarnation,
                  entries: tuple[BoardEntry, ...]) -> tuple[ReaderBoardDelivery, ...]:
        """Delivery evidence for rows no longer on the board."""
        current = {entry.content for entry in entries}
        return tuple(delivered for delivered in cls.select(db, reader=cls.column(reader))
                     if delivered.content not in current)

    @classmethod
    def deliver(cls, root: Path, reader: ThreadIncarnation, entries: tuple[BoardEntry, ...],
                now: float) -> SessionBoard:
        """Read first, and take Core's writer only when a row is due or evidence went stale, so a
        quiet tool call never contends for coordination.sqlite3. The writer re-selects and stamps
        in one transaction, so two concurrent calls cannot both deliver one change. A busy writer
        delivers nothing on this call: its rows stay unstamped and print on the next call."""
        recorded = reader.require_recorded()
        with (
            MaintenanceBarrier(RegistryFile.path_in(root)).allow_input(),
            CoordinationSession(str(CoordinationStore.path_in(root))) as store,
        ):
            with store.read():
                db = store._connection
                cls.require_schema(db)
                if not (cls.due(db, recorded, entries, now) or cls.forgotten(db, recorded, entries)):
                    return SessionBoard.presented((), now)
            try:
                with store.writing() as db:
                    for delivered in cls.forgotten(db, recorded, entries):
                        cls.delete(db, reader=delivered.reader, content=delivered.content)
                    due = cls.due(db, recorded, entries, now)
                    for entry in due:
                        cls(cls.column(recorded), entry.content, entry.since, now).upsert(db)
            except CoordinationBeginUnavailable:
                return SessionBoard.presented((), now)
        return SessionBoard.presented(due, now)

    @classmethod
    def lines(cls, comms: Comms, reader: ThreadIncarnation, now: float) -> tuple[str, ...]:
        """The rows to print on this reader's tool call at ``now``, stamped in the same call
        that returns them: a row is never marked delivered to a reader that did not get it."""
        return cls.deliver(comms.root, reader, TeamUpdate.current_board(comms, reader), now).lines
