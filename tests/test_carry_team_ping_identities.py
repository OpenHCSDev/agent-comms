"""The real carry re-keys retained deaf lines to their thread's birth and keeps their start."""

from cutover.carry_team_ping_identities import TeamPingIdentities

from agent_comms.comms import wire
from agent_comms.locked_store import LiveFilesRequest
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.text_digest import TextDigest
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Checkout
from agent_comms.task_attachments.team_pings import RulePing, TeamUpdate, TeamUpdateNotice, TeamUpdateStore
from agent_comms.team.standing import TeamDeaf, UnitDeaf
from agent_comms.threads import Thread
from conftest import maintenance_held


def test_retained_deaf_lines_keep_their_start_under_their_birth(tmp_path):
    comms = wire(tmp_path)
    for name, born in (("lead", 1.0), ("worker", 2.0)):
        comms.registry.declare(Thread(name, frozenset(), Checkout(tmp_path), created_at=born))
    lead = comms.registry.require("lead").incarnation
    worker = comms.registry.require("worker").incarnation.require_recorded()
    started = RecordedEventTime(100.0)
    retired = (  # the digests the previous runtime stored: no teammate, a unit by name
        RulePing(TeamDeaf, TextDigest(FieldCodec.comparison_digest((TeamDeaf, ()))),
                 text="deaf", since=started),
        RulePing(UnitDeaf, TextDigest(FieldCodec.comparison_digest((UnitDeaf, ("worker",)))),
                 text="undeafen", since=started),
    )
    TeamUpdateStore.record(tmp_path, TeamUpdateNotice(lead, retired))
    step = TeamPingIdentities(LiveFilesRequest(tmp_path), tmp_path / "spill")

    assert step.carry(apply=False).changed == 1
    with maintenance_held(tmp_path):
        assert len(step.carry(apply=True).rewritten) == 1
    packet, = TeamUpdate.latest(comms, lead)
    assert tuple((ping.key, ping.since) for ping in packet.handled) == (
        (TeamDeaf(0.0, lead.require_recorded()).observation_key(), started),
        (UnitDeaf(0.0, worker).observation_key(), started),
    )
    assert step.carry(apply=False).changed == 0
