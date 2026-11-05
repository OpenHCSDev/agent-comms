"""Which board rows have reached one reader: delivery evidence, not a copy of Core's board."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.comms import Comms
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.schema import CoordinatorRow
from agent_comms.coordination.session import CoordinationSession
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier
from agent_comms.task_attachments.team_pings import TeamUpdate
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.storage.typed_table import Column
from agent_comms.transcript_routes import WithoutRowid
from agent_comms_hooks.board import BoardEntry, SessionBoard

from agent_comms.storage.locked_store import RegistryFile


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

    @classmethod
    def deliver(cls, root: Path, reader: ThreadIncarnation, entries: tuple[BoardEntry, ...],
                now: float) -> SessionBoard:
        """Select and stamp in one transaction: two concurrent calls cannot both deliver one
        change, and a row no longer on the board forgets that it was delivered."""
        lookup = str(reader.require_recorded().lookup)
        due: list[BoardEntry] = []
        with (
            MaintenanceBarrier(RegistryFile.path_in(root)).allow_input(),
            CoordinationSession(str(CoordinationStore.path_in(root))) as store,
            store.writing() as db,
        ):
            cls.require_schema(db)
            current = {entry.content for entry in entries}
            for delivered in cls.select(db, reader=lookup):
                if delivered.content not in current:
                    cls.delete(db, reader=lookup, content=delivered.content)
            for entry in entries:
                prior = cls.select(db, reader=lookup, content=entry.content, since=entry.since)
                if all(delivered.alarm_due(entry, now) for delivered in prior):
                    cls(lookup, entry.content, entry.since, now).upsert(db)
                    due.append(entry)
        return SessionBoard.presented(due, now)

    @classmethod
    def lines(cls, comms: Comms, reader: ThreadIncarnation, now: float) -> tuple[str, ...]:
        """The rows to print on this reader's tool call at ``now``, stamped in the same call
        that returns them: a row is never marked delivered to a reader that did not get it."""
        return cls.deliver(comms.root, reader, TeamUpdate.current_board(comms, reader), now).lines
