"""One team board owns each line's age: every teammate's retained packet dates it."""

from dataclasses import replace

from agent_comms.comms import wire
from agent_comms.heartbeat_subjects import TeamHistory
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Checkout
from agent_comms.task_attachments.team_pings import RulePing, TeamUpdate, TeamUpdateNotice
from agent_comms.team.standing import TeamDeaf
from agent_comms.threads import Thread


def test_a_teammate_with_no_packet_inherits_the_boards_earliest_start(tmp_path):
    comms = wire(tmp_path)
    for name, born in (("lead", 1.0), ("peer", 2.0), ("newcomer", 3.0)):
        comms.registry.declare(Thread(name, frozenset(), Checkout(tmp_path), created_at=born))
    lead, peer, newcomer = (comms.registry.require(name).incarnation
                            for name in ("lead", "peer", "newcomer"))
    rule = TeamDeaf(900.0, lead.require_recorded())
    line = RulePing.capture(rule)
    retained = (TeamUpdateNotice(lead, (replace(line, since=RecordedEventTime(100.0)),)),
                TeamUpdateNotice(peer, (replace(line, since=RecordedEventTime(500.0)),)))
    delivery = TeamUpdate.derive(newcomer, (), (rule,), retained, published=(), now=1000.0,
                                 history=TeamHistory.read(comms, frozenset()))
    handled, = delivery.packet.handled
    assert handled.since == RecordedEventTime(100.0)
