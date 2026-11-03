"""Keep the retained deaf lines as unbound records, with their original digest and start.

TeamDeaf watched the board's one lead with no compared field, and UnitDeaf named its unit by
string. Both now name the thread by birth. No stored evidence names the birth behind an old
row: the lead's board fanned out to every teammate, the registry's lead at the cutover is not
the lead of the row's epoch, and a name may have passed to another thread. So every retained
row of either rule, all written by the previous runtime, becomes an UnboundRulePing, which
the board keeps beside its current captures while its rule still has a current line.
Published copies are not rewritten. Delete this step after the live installation.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import ClassVar

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RegistryFile, RootFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.standing_rules import StandingRule
from agent_comms.store_files import AtomicFile
from agent_comms.task_attachments.team_pings import TeamUpdate, TeamUpdateStore
from agent_comms.team.standing import TeamDeaf, UnitDeaf


class TeamPingIdentities(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (TeamUpdateStore,)
    retired: ClassVar[tuple[type[StandingRule], ...]] = (TeamDeaf, UnitDeaf)

    def carry(self, *, apply: bool) -> Carried:
        rewritten = []
        for path in sorted(TeamUpdateStore.matching_in(self.root)):
            self.require_confined(path)
            stored = FieldCodec.decode(tuple[TeamUpdate, ...], FieldCodec.parse(path.read_text()))
            carried = tuple(replace(packet, handled=tuple(
                ping.unbound(self.retired) for ping in packet.handled)) for packet in stored)
            if carried == stored:  # an unbound line never equals the rule line it was
                continue
            if apply:
                MaintenanceBarrier(RegistryFile.path_in(self.root)).read().require_held_by(
                    MaintenanceHolder.current(), "Keep retained deaf lines unbound")
                AtomicFile(path).write_text(json.dumps(FieldCodec.encode(carried)))
            rewritten.append(path.relative_to(self.root))
        return Carried(len(rewritten), rewritten=tuple(rewritten) if apply else ())
