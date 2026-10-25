"""Drop the retired HUD rows from stored commander updates and give each decisions packet its
undisposed tail. The HUD owns those rows now; the stored packets keep only heartbeat lines.
Delete this step after the next installation.
"""

from __future__ import annotations

import json
import sqlite3
from abc import abstractmethod
from contextlib import closing
from dataclasses import dataclass, field
from typing import ClassVar, Self

from agent_comms.bus import Bus
from agent_comms.task_attachments.commander_pings import CommanderDecisions, CommanderNotice, CommanderUpdate, Ping
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.messages import Message
from agent_comms.thread_identity import ThreadIncarnation

# The wire names of the deleted HUD ping classes; no class declares them any more.
RETIRED = ("load", "unit_row", "phase_row", "finished_phases_row", "forks_row")


@dataclass(frozen=True)
class StoredPacket:
    """One stored packet with its retired HUD rows removed in SQLite, before any decode."""

    seq: int
    task: str
    removed: bool

    @classmethod
    def read(cls, db: sqlite3.Connection, seq: int, task: str) -> Self:
        rows = tuple(path for path, in db.execute(
            "SELECT fullkey FROM json_tree(?) WHERE type = 'object'"
            f" AND json_extract(value, '$.kind') IN ({', '.join('?' * len(RETIRED))})"
            " ORDER BY id DESC", (task, *RETIRED)))
        # Later array elements go first, so each removal leaves the earlier paths valid.
        kept, = db.execute(f"SELECT json_remove(?{', ?' * len(rows)})", (task, *rows)).fetchone()
        return cls(seq, kept, bool(rows))


@dataclass(frozen=True)
class CommanderUpdateStored(DeclaredFamily, affix="Stored"):
    """One commander update as its packet was stored before it carried undisposed lines."""

    commander: ThreadIncarnation
    handled: tuple[Ping, ...]
    not_applied: tuple[str, ...] = field(default=(), kw_only=True)

    @abstractmethod
    def carried(self) -> CommanderUpdate: ...

    def stale(self, packet: StoredPacket) -> bool:
        return packet.removed

    @classmethod
    def packets(cls, db: sqlite3.Connection) -> tuple[tuple[StoredPacket, Self], ...]:
        table = Message.table
        tail = f"$.{FieldCodec.record_key(CommanderDecisions, 'waiting')}"
        stored = db.execute(table._sql(
            f'SELECT {{seq}}, {{task}} FROM "{table.declared_name}"'
            " WHERE json_extract({task}, '$.kind') = ? AND json_type({task}, ?) IS NULL"),
            (cls.declared_name, tail)).fetchall()
        packets = (StoredPacket.read(db, seq, task) for seq, task in stored)
        return tuple((packet, FieldCodec.decode(cls, json.loads(packet.task)))
                     for packet in packets)


@dataclass(frozen=True)
class CommanderDecisionsStored(CommanderUpdateStored):
    decisions: tuple[Ping, ...]

    def carried(self) -> CommanderUpdate:
        return CommanderDecisions(self.commander, self.handled, self.decisions, (),
                                  not_applied=self.not_applied)

    def stale(self, packet: StoredPacket) -> bool:
        """Only packets without the undisposed tail are selected, and each lacks it."""
        return True


@dataclass(frozen=True)
class CommanderNoticeStored(CommanderUpdateStored):
    def carried(self) -> CommanderUpdate:
        return CommanderNotice(self.commander, self.handled, not_applied=self.not_applied)


class CommanderUpdateLines(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (Bus,)

    def carry(self, *, apply: bool) -> Carried:
        table = Message.table
        column, = table._fields_named(("task",))
        rewritten = []
        changed = 0
        for path in self.input_files():
            with closing(Bus.read_snapshot(path)) as db:
                updates: list[tuple[object, int]] = [
                    (json.dumps(FieldCodec.encode(update.carried())), packet.seq)
                    for member in CommanderUpdateStored.members_with(CommanderUpdateStored)
                    for packet, update in member.packets(db) if update.stale(packet)
                ]
            changed += len(updates)
            if apply and updates:
                with closing(Bus.writer(path)) as db, Bus.write_transaction(db):
                    self.rewrite_stored(db, table, column, updates)
                rewritten.append(path.relative_to(self.root))
        return Carried(changed, detail={"packets": changed}, rewritten=tuple(rewritten))
