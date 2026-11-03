"""The real carry keeps every retained deaf line as an unbound record: no stored evidence
names the birth behind it, so its original digest and start are all it keeps."""

from cutover.carry_team_ping_identities import TeamPingIdentities

from agent_comms.comms import wire
from agent_comms.locked_store import LiveFilesRequest
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.repo import Checkout
from agent_comms.task_attachments.team_pings import (
    RulePing, TeamUpdate, TeamUpdateNotice, TeamUpdateStore, UnboundRulePing,
)
from agent_comms.team.standing import TeamDeaf, UnitDeaf
from agent_comms.text_digest import TextDigest
from agent_comms.threads import Thread
from conftest import maintenance_held


def retired(rule, *values):
    """The digest the previous runtime stored: TeamDeaf compared nothing, UnitDeaf a name."""
    return TextDigest(FieldCodec.comparison_digest((rule, values)))


def test_every_retained_deaf_line_becomes_unbound_with_its_digest_and_start(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("lead", frozenset(), Checkout(tmp_path), created_at=1.0))
    lead = comms.registry.require("lead").incarnation
    dated, undated = RecordedEventTime(100.0), UnrecordedEventTime()
    TeamUpdateStore.record(tmp_path, TeamUpdateNotice(lead, (
        RulePing(TeamDeaf, retired(TeamDeaf), text="deaf", since=dated),
        RulePing(UnitDeaf, retired(UnitDeaf, "worker"), text="undeafen", since=undated),
    )))
    step = TeamPingIdentities(LiveFilesRequest(tmp_path), tmp_path / "spill")

    assert step.carry(apply=False).changed == 1
    with maintenance_held(tmp_path):
        assert len(step.carry(apply=True).rewritten) == 1
    packet, = TeamUpdate.latest(comms, lead)
    assert packet.handled == (UnboundRulePing(TeamDeaf, retired(TeamDeaf), text=""),
                              UnboundRulePing(UnitDeaf, retired(UnitDeaf, "worker"), text=""))
    assert tuple(ping.since for ping in packet.handled) == (dated, undated)
    assert all(UnboundRulePing.marker in ping.text for ping in packet.handled)
    assert step.carry(apply=False).changed == 0
