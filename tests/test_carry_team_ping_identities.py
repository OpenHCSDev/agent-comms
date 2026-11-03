"""The real carry binds retained deaf lines only where original evidence names the thread."""

import json

import pytest
from cutover.carry_team_ping_identities import TeamPingIdentities

from agent_comms.comms import wire
from agent_comms.locked_store import LiveFilesRequest, RegistryFile
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Checkout
from agent_comms.task_attachments.team_pings import RulePing, TeamUpdate, TeamUpdateNotice, TeamUpdateStore
from agent_comms.team.standing import TeamDeaf, UnitDeaf
from agent_comms.text_digest import TextDigest
from agent_comms.threads import Thread
from conftest import maintenance_held

STARTED = RecordedEventTime(100.0)


def retired(rule, *values):
    """The digest the previous runtime stored: TeamDeaf compared nothing, UnitDeaf a name."""
    return TextDigest(FieldCodec.comparison_digest((rule, values)))


def stored_lead(root, lead) -> None:
    """Write the version-1 registry an installed runtime left: the team's stored lead."""
    path = RegistryFile.path_in(root)
    document = json.loads(path.read_text())
    document["version"] = 1
    document["commander_project"] = {
        "kind": "registered", "repos": [],
        "commander": {"name": lead.name, "created_at": lead.created_at}}
    path.write_text(json.dumps(document, indent=2))


def carried(root, packets):
    for packet in packets:
        TeamUpdateStore.record(root, packet)
    step = TeamPingIdentities(LiveFilesRequest(root), root / "spill")
    with maintenance_held(root):
        step.carry(apply=True)
    assert step.carry(apply=False).changed == 0
    comms = wire(root)
    return {packet.commander.name: tuple((ping.key, ping.since) for ping in packet.handled)
            for owner in packets for packet in TeamUpdate.latest(comms, owner.commander)}


@pytest.fixture
def team(tmp_path):
    comms = wire(tmp_path)
    # worker was born before the deaf line began; replaced was born after it (a replacement).
    for name, born in (("lead", 1.0), ("peer", 2.0), ("worker", 3.0), ("replaced", 500.0)):
        comms.registry.declare(Thread(name, frozenset(), Checkout(tmp_path), created_at=born))
    return comms


def test_the_boards_deaf_line_binds_to_its_stored_lead_in_every_teammates_packet(team, tmp_path):
    lead, peer = (team.registry.require(name).incarnation for name in ("lead", "peer"))
    stored_lead(tmp_path, lead)
    deaf = RulePing(TeamDeaf, retired(TeamDeaf), text="deaf", since=STARTED)
    lines = carried(tmp_path, (TeamUpdateNotice(lead, (deaf,)), TeamUpdateNotice(peer, (deaf,))))
    # The peer received the lead's board: its line is the lead's deafness, never the peer's.
    bound = ((TeamDeaf(0.0, lead.require_recorded()).observation_key(), STARTED),)
    assert lines == {"lead": bound, "peer": bound}


def test_a_unit_line_binds_only_to_a_thread_born_before_it_began(team, tmp_path):
    lead = team.registry.require("lead").incarnation
    worker = team.registry.require("worker").incarnation.require_recorded()
    lines = carried(tmp_path, (TeamUpdateNotice(lead, (
        RulePing(UnitDeaf, retired(UnitDeaf, "worker"), text="worker", since=STARTED),
        RulePing(UnitDeaf, retired(UnitDeaf, "replaced"), text="replaced", since=STARTED),
        RulePing(TeamDeaf, retired(TeamDeaf), text="deaf", since=STARTED),
    )),))
    assert lines == {"lead": (
        (UnitDeaf(0.0, worker).observation_key(), STARTED),
        (retired(UnitDeaf, "replaced"), STARTED),  # born after the line: may be a replacement
        (retired(TeamDeaf), STARTED),  # no stored lead in this registry: unbound
    )}
