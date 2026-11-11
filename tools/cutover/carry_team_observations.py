"""Seed observations from the indexed packets that owned the previous heartbeat baseline.

A disposable HUD has no durable capture order and can precede a later failure publication.
It must not replace that baseline. Existing observation stores are never overwritten, and
original messages are unchanged; only the heartbeat advances the seeded state afterward.
"""

from __future__ import annotations

from contextlib import closing
from typing import ClassVar

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.messages import TeamUpdatePosition
from agent_comms.nominal.field_codec import FieldCodec, ThreadLookup
from agent_comms.storage.locked_store import RootFile
from agent_comms.task_attachments.team_pings import TeamUpdate, TeamUpdateStore


class TeamObservations(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (
        Bus, TeamUpdateStore,
    )

    def carry(self, *, apply: bool) -> Carried:
        packets: dict[ThreadLookup, TeamUpdate] = {}
        for path in Bus.matching_in(self.root):
            with closing(Bus.read_snapshot(path)) as db:
                lookups = db.execute(TeamUpdatePosition._sql(
                    'SELECT DISTINCT {teammate_lookup} FROM '
                    f'"{TeamUpdatePosition.declared_name}"'))
                for raw, in lookups:
                    lookup = FieldCodec.decode(ThreadLookup, raw)
                    for message in TeamUpdatePosition.latest(db, lookup):
                        for packet in message.task.team_update(lookup):
                            packets[lookup] = packet
        missing = tuple(packet for packet in packets.values()
                        if not TeamUpdateStore.for_team(self.root, packet.teammate).path.exists())
        if apply:
            for packet in missing:
                TeamUpdateStore.record(self.root, packet)
        return Carried(len(missing), detail={"observations": len(missing)}, rewritten=tuple(
            TeamUpdateStore.for_team(self.root, packet.teammate).path.relative_to(self.root)
            for packet in missing
        ) if apply else ())
