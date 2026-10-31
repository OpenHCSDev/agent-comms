"""Seed retained observations once from existing packets, without changing original messages.

Current HUD frames may be newer than the latest quiet publication, so their packet wins.
After seeding, only the heartbeat's observation writer advances this state. No load-time
converter, old-format reader or hand-mapped packet schema remains in the running reader.
"""

from __future__ import annotations

from contextlib import closing
from typing import ClassVar

from agent_comms.bus import Bus
from agent_comms.commander_hud import CommanderFrame
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.messages import CommanderUpdatePosition
from agent_comms.nominal.field_codec import FieldCodec, ThreadLookup
from agent_comms.task_attachments.commander_pings import CommanderUpdate, CommanderUpdateStore


class CommanderObservations(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (
        Bus, CommanderFrame, CommanderUpdateStore,
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
        for path in CommanderFrame.matching_in(self.root):
            for packet in CommanderFrame(path).read().packets:
                packets[packet.commander.require_recorded().lookup] = packet
        missing = tuple(packet for packet in packets.values()
                        if not CommanderUpdateStore.at(self.root, packet.commander).path.exists())
        if apply:
            for packet in missing:
                CommanderUpdateStore.record(self.root, packet)
        return Carried(len(missing), detail={"observations": len(missing)}, rewritten=tuple(
            CommanderUpdateStore.at(self.root, packet.commander).path.relative_to(self.root)
            for packet in missing
        ) if apply else ())
