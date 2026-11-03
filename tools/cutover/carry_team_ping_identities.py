"""Re-key the retained deaf lines to their teammate's birth, so their age crosses the cutover.

TeamDeaf watched the one lead with no compared field, and UnitDeaf named its unit by string.
Both now name the thread by birth, so each retained line's observation digest changed and
the board would date it afresh. Each retained TeamDeaf line is the lead whose packet holds
it; each UnitDeaf line names a registered thread. Both keep their start. Published copies
are not rewritten: such a line is announced once more, with its original age. Delete this
step after the live installation.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import ClassVar

from pydantic import JsonValue

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RegistryFile, RootFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.text_digest import TextDigest
from agent_comms.store_files import AtomicFile
from agent_comms.task_attachments.team_pings import TeamUpdate, TeamUpdateStore
from agent_comms.team.standing import TeamDeaf, UnitDeaf
from agent_comms.thread_identity import RecordedThreadIncarnation


class TeamPingIdentities(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RegistryFile, TeamUpdateStore)

    @staticmethod
    def retired(rule: type, *values: object) -> TextDigest:
        """A line's digest as the previous runtime computed it: its type and compared values."""
        return TextDigest(FieldCodec.comparison_digest((rule, values)))

    def births(self) -> dict[TextDigest, RecordedThreadIncarnation]:
        """Each registered thread's retired UnitDeaf digest, to its birth. The thread rows read
        the same in every registry version, so this needs no registry decoder."""
        path = RegistryFile.path_in(self.root)
        document = FieldCodec.decode(dict[str, JsonValue], FieldCodec.parse(path.read_text()))
        threads = FieldCodec.decode(dict[str, dict[str, JsonValue]], document["threads"])
        return {self.retired(UnitDeaf, name):
                RecordedThreadIncarnation(name, FieldCodec.decode(float, row["created_at"]))
                for name, row in threads.items()}

    def rekeyed(self, packet: TeamUpdate,
                births: dict[TextDigest, RecordedThreadIncarnation]) -> TeamUpdate:
        lead = self.retired(TeamDeaf)
        current: dict[TextDigest, TextDigest] = {
            lead: TeamDeaf(0.0, packet.commander.require_recorded()).observation_key(),
            **{old: UnitDeaf(0.0, birth).observation_key() for old, birth in births.items()},
        }
        # A rule line's key is its observation digest; no other line's key is one of these.
        return replace(packet, handled=tuple(
            replace(ping, observation=current[ping.key]) if ping.key in current else ping
            for ping in packet.handled))

    def carry(self, *, apply: bool) -> Carried:
        registry = RegistryFile.path_in(self.root)
        if not registry.exists():
            return Carried(0)
        births = self.births()
        rewritten = []
        for path in sorted(TeamUpdateStore.matching_in(self.root)):
            self.require_confined(path)
            stored = FieldCodec.decode(tuple[TeamUpdate, ...], FieldCodec.parse(path.read_text()))
            carried = tuple(self.rekeyed(packet, births) for packet in stored)
            if carried == stored:  # a rule line's equality includes its observation
                continue
            if apply:
                MaintenanceBarrier(registry).read().require_held_by(
                    MaintenanceHolder.current(), "Re-key retained deaf lines")
                AtomicFile(path).write_text(json.dumps(FieldCodec.encode(carried)))
            rewritten.append(path.relative_to(self.root))
        return Carried(len(rewritten), rewritten=tuple(rewritten) if apply else ())
