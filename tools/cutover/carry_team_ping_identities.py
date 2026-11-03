"""Bind retained deaf lines to their thread's birth from original evidence, or leave them unbound.

TeamDeaf watched the board's one lead with no compared field, and UnitDeaf named its unit by
string. Both now name the thread by birth, so each retained line's observation digest changed.
A line is re-keyed only where original evidence names its thread:

- TeamDeaf watched the board's lead, and the version-1 registry stores that lead
  (``commander_project.commander``). The lead's board fanned out to every teammate, so a
  packet's own recipient is never the evidence; the stored lead is. This step therefore runs
  before the registry carry drops that field.
- UnitDeaf named a thread that existed when the line began: a registered thread binds the
  line only when it was born no later than the line's recorded start. A thread born after
  it, or a line with no recorded start, may be a replacement under the same name, so the
  line stays unbound.

An unbound line keeps its original digest and start. Published copies are not rewritten.
Delete this step after the live installation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import ClassVar

from pydantic import JsonValue

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RegistryFile, RootFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry_document import RegistryDocument
from agent_comms.recorded_time import EventTime
from agent_comms.store_files import AtomicFile
from agent_comms.task_attachments.team_pings import Ping, TeamUpdate, TeamUpdateStore
from agent_comms.team.standing import TeamDeaf, UnitDeaf
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.threads import Thread
from cutover.carry_team_project_lead import TeamProjectLead


@dataclass(frozen=True)
class Unwitnessed:
    """No original evidence names this line's thread: it keeps its digest and start."""

    def bound(self, ping: Ping) -> Ping:
        return ping


@dataclass(frozen=True)
class LeadWitness(Unwitnessed):
    """The stored lead is the subject of the board's one TeamDeaf line, whoever received it."""

    current: TextDigest

    def bound(self, ping: Ping) -> Ping:
        return ping.observed_as(self.current)


@dataclass(frozen=True)
class BirthWitness(Unwitnessed):
    """A thread that already existed when the line began binds it; a later birth does not."""

    birth: RecordedThreadIncarnation
    current: TextDigest

    def bound(self, ping: Ping) -> Ping:
        born = EventTime.capture(self.birth.created_at).chronology()
        return ping.observed_as(self.current) if born <= ping.since.chronology() else ping


class TeamPingIdentities(Carry):
    precedes = (TeamProjectLead,)
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RegistryFile, TeamUpdateStore)

    @staticmethod
    def retired(rule: type, *values: object) -> TextDigest:
        """A line's digest as the previous runtime computed it: its type and compared values."""
        return TextDigest(FieldCodec.comparison_digest((rule, values)))

    def witnesses(self) -> dict[object, Unwitnessed]:
        """Each retired digest original evidence can bind, read raw from the registry: its
        thread rows read the same in every version, and only version 1 stores a lead."""
        path = RegistryFile.path_in(self.root)
        document = FieldCodec.decode(dict[str, JsonValue], FieldCodec.parse(path.read_text()))
        threads = FieldCodec.decode(
            dict[str, Thread], document[FieldCodec.record_key(RegistryDocument, "threads")])
        witnesses: dict[object, Unwitnessed] = {}
        for thread in threads.values():
            born = thread.incarnation.require_recorded()
            witnesses[self.retired(UnitDeaf, born.name)] = BirthWitness(
                born, UnitDeaf(0.0, born).observation_key())
        if TeamProjectLead.retired_key in document:  # a version-1 registry with a team
            project = FieldCodec.decode(dict[str, JsonValue], document[TeamProjectLead.retired_key])
            lead = FieldCodec.decode(RecordedThreadIncarnation, project[TeamProjectLead.retired_field])
            witnesses[self.retired(TeamDeaf)] = LeadWitness(TeamDeaf(0.0, lead).observation_key())
        return witnesses

    def carry(self, *, apply: bool) -> Carried:
        registry = RegistryFile.path_in(self.root)
        if not registry.exists():
            return Carried(0)
        witnesses = self.witnesses()
        rewritten = []
        for path in sorted(TeamUpdateStore.matching_in(self.root)):
            self.require_confined(path)
            stored = FieldCodec.decode(tuple[TeamUpdate, ...], FieldCodec.parse(path.read_text()))
            # A rule line's key is its observation digest; no other line's key is one of these.
            carried = tuple(replace(packet, handled=tuple(
                witnesses.get(ping.key, Unwitnessed()).bound(ping) for ping in packet.handled))
                for packet in stored)
            if carried == stored:  # a rule line's equality includes its observation
                continue
            if apply:
                MaintenanceBarrier(registry).read().require_held_by(
                    MaintenanceHolder.current(), "Bind retained deaf lines")
                AtomicFile(path).write_text(json.dumps(FieldCodec.encode(carried)))
            rewritten.append(path.relative_to(self.root))
        return Carried(len(rewritten), rewritten=tuple(rewritten) if apply else ())
