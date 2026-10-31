"""Seed observations from the indexed packets that owned the previous heartbeat baseline.

A disposable HUD has no durable capture order and can precede a later failure publication.
It must not replace that baseline. Existing observation stores are never overwritten, and
original messages are unchanged; only the heartbeat advances the seeded state afterward.
"""

from __future__ import annotations

from contextlib import closing
from typing import ClassVar

from agent_comms.bus import Bus
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.messages import CommanderUpdatePosition
from agent_comms.nominal.field_codec import FieldCodec, ThreadLookup
from agent_comms.task_attachments.commander_pings import CommanderUpdate, CommanderUpdateStore


class CommanderObservations(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (
        Bus, CommanderUpdateStore,
    )

    def carry(self, *, apply: bool) -> Carried:
        packets: dict[ThreadLookup, CommanderUpdate] = {}
        for path in Bus.matching_in(self.root):
            with closing(Bus.read_snapshot(path)) as db:
                lookups = db.execute(CommanderUpdatePosition._sql(
                    'SELECT DISTINCT {commander_lookup} FROM '
                    f'"{CommanderUpdatePosition.declared_name}"'))
                for raw, in lookups:
                    lookup = FieldCodec.decode(ThreadLookup, raw)
                    for message in CommanderUpdatePosition.latest(db, lookup):
                        for packet in message.task.commander_update(lookup):
                            packets[lookup] = packet
        missing = tuple(packet for packet in packets.values()
                        if not CommanderUpdateStore.at(self.root, packet.commander).path.exists())
        if apply:
            for packet in missing:
                CommanderUpdateStore.record(self.root, packet)
        return Carried(len(missing), detail={"observations": len(missing)}, rewritten=tuple(
            CommanderUpdateStore.at(self.root, packet.commander).path.relative_to(self.root)
            for packet in missing
        ) if apply else ())
