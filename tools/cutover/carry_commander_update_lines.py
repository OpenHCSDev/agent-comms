"""Drop the retired HUD rows from stored commander updates and give each decisions packet its
undisposed tail. The HUD owns those rows now; the stored packets keep only heartbeat lines.
Delete this step after the next installation.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from contextlib import closing
from dataclasses import dataclass, field
from typing import ClassVar

from agent_comms.bus import Bus
from agent_comms.commander_pings import CommanderDecisions, CommanderNotice, CommanderUpdate, Ping
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.messages import Message
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.typed_table import TypedRow


@dataclass(frozen=True)
class RetiredHudPing(Ping, declared_name=None):
    """The HUD rows stored packets still carry, decoded only to be dropped."""

    @property
    def key(self) -> str:
        return self.text


@dataclass(frozen=True)
class UnitRowPing(RetiredHudPing):
    unit: ThreadIncarnation


@dataclass(frozen=True)
class PhaseRowPing(RetiredHudPing):
    phase: str


@dataclass(frozen=True)
class FinishedPhasesRowPing(RetiredHudPing):
    plan: str


@dataclass(frozen=True)
class ForksRowPing(RetiredHudPing):
    pass


@dataclass(frozen=True)
class LoadPing(RetiredHudPing):
    pass


def lines(pings: tuple[Ping, ...]) -> tuple[Ping, ...]:
    return tuple(ping for ping in pings if not isinstance(ping, RetiredHudPing))


@dataclass(frozen=True)
class StoredUpdate(DeclaredFamily, affix="Stored"):
    """One commander update as its packet was stored before the HUD rows left it."""

    commander: ThreadIncarnation
    handled: tuple[Ping, ...]
    not_applied: tuple[str, ...] = field(default=(), kw_only=True)

    @abstractmethod
    def carried(self) -> CommanderUpdate: ...

    def stale(self) -> bool:
        return lines(self.handled) != self.handled


@dataclass(frozen=True)
class StoredDecisions(StoredUpdate, declared_name=CommanderDecisions.declared_name):
    decisions: tuple[Ping, ...]

    def stale(self) -> bool:
        """Only packets without the undisposed tail are selected, and each lacks it."""
        return True

    def carried(self) -> CommanderUpdate:
        return CommanderDecisions(self.commander, lines(self.handled), lines(self.decisions), (),
                                  not_applied=self.not_applied)


@dataclass(frozen=True)
class StoredNotice(StoredUpdate, declared_name=CommanderNotice.declared_name):
    def carried(self) -> CommanderUpdate:
        return CommanderNotice(self.commander, lines(self.handled), not_applied=self.not_applied)


@dataclass(frozen=True)
class StoredPacket(TypedRow):
    seq: int
    task: StoredUpdate


class CommanderUpdateLines(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (Bus,)

    def carry(self, *, apply: bool) -> Carried:
        table = Message.table
        column, = (item for item in table._fields if item.name == "task")
        rewritten = []
        changed = 0
        for path in self.input_files():
            with closing(Bus.read_snapshot(path)) as db:
                stored = tuple(
                    packet
                    for member in StoredUpdate.members_with(StoredUpdate)
                    for packet in StoredPacket.read(db.execute(table._sql(
                        f'SELECT {{seq}}, {{task}} FROM "{table.declared_name}"'
                        " WHERE json_extract({task}, '$.kind') = ? AND json_type({task}, ?) IS NULL"),
                        (member.declared_name, f"$.{FieldCodec.record_key(CommanderDecisions, 'waiting')}")))
                )
            updates = [(json.dumps(FieldCodec.encode(packet.task.carried())), packet.seq)
                       for packet in stored if packet.task.stale()]
            changed += len(updates)
            if apply and updates:
                with closing(Bus.writer(path)) as db, Bus.write_transaction(db):
                    self.rewrite_stored(db, table, column, updates)
                rewritten.append(path.relative_to(self.root))
        return Carried(changed, detail={"packets": changed}, rewritten=tuple(rewritten))
