"""Keep the retained deaf lines as unbound observations, with their original digest and start.

TeamDeaf watched the board's one lead with no compared field, and UnitDeaf named its unit by
string. Both now name the thread by birth. No stored evidence names the birth behind an old
row: the lead's board fanned out to every teammate, the registry's lead at the cutover is not
the lead of the row's epoch, and a name may have passed to another thread. So every retained
row of either rule, all written by the previous runtime, is read back as an UnboundRulePing,
which the board keeps as an observation only until it is explicitly retired. This is the
decoder for that retired format: it rewrites only each such row's member before the store's
own decoder reads it. Published copies are not rewritten. Delete this step after the live
installation.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import JsonValue

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RegistryFile, RootFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.store_files import AtomicFile
from agent_comms.task_attachments.team_pings import (
    Ping, RulePing, TeamUpdate, TeamUpdateNotice, TeamUpdateStore, UnboundRulePing,
)
from agent_comms.team.standing import TeamDeaf, UnitDeaf


class TeamPingIdentities(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (TeamUpdateStore,)
    retired: ClassVar[frozenset[str]] = frozenset(
        rule.declared_name for rule in (TeamDeaf, UnitDeaf))
    """The wire names of the rules whose rows the previous runtime wrote without a birth."""

    def read_back(self, line: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """One stored line in the current family: a retired rule's row becomes unbound."""
        member = Ping.family_discriminator
        retired = (line[member] == RulePing.declared_name
                   and line[FieldCodec.record_key(RulePing, "rule")] in self.retired)
        return {**line, member: UnboundRulePing.declared_name} if retired else line

    def carry(self, *, apply: bool) -> Carried:
        handled = FieldCodec.record_key(TeamUpdateNotice, "handled")
        rewritten = []
        for path in sorted(TeamUpdateStore.matching_in(self.root)):
            self.require_confined(path)
            stored = FieldCodec.decode(
                list[dict[str, JsonValue]], FieldCodec.parse(path.read_text()))
            carried = [{**packet, handled: [
                self.read_back(FieldCodec.decode(dict[str, JsonValue], line))
                for line in FieldCodec.decode(list[JsonValue], packet[handled])]}
                for packet in stored]
            if carried == stored:
                continue
            FieldCodec.decode(tuple[TeamUpdate, ...], carried)  # the store's decoder reads it
            if apply:
                MaintenanceBarrier(RegistryFile.path_in(self.root)).read().require_held_by(
                    MaintenanceHolder.current(), "Keep retained deaf lines unbound")
                AtomicFile(path).write_text(json.dumps(carried))
            rewritten.append(path.relative_to(self.root))
        return Carried(len(rewritten), rewritten=tuple(rewritten) if apply else ())
