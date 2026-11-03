"""One team board owns each line's age: every teammate's retained packet dates it."""

from dataclasses import replace

from agent_comms.comms import wire
from agent_comms.heartbeat_subjects import TeamHistory
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Checkout
from agent_comms.task_attachments.team_pings import (
    RulePing, TeamUpdate, TeamUpdateNotice, UnboundRulePing,
)
from agent_comms.team.standing import TeamDeaf
from agent_comms.text_digest import TextDigest
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


def test_an_unbound_line_lives_beside_its_rules_captures_and_leaves_with_them(tmp_path):
    """The first and every later pass keep the pre-cutover line, with its start, beside the
    fresh capture it cannot match; a pass with no line of its rule drops it."""
    comms = wire(tmp_path)
    for name, born in (("lead", 1.0), ("peer", 2.0)):
        comms.registry.declare(Thread(name, frozenset(), Checkout(tmp_path), created_at=born))
    lead, peer = (comms.registry.require(name).incarnation for name in ("lead", "peer"))
    history = TeamHistory.read(comms, frozenset())
    unbound = UnboundRulePing(TeamDeaf, TextDigest("0" * 64), text="old deaf",
                              since=RecordedEventTime(100.0))
    current = TeamDeaf(900.0, peer.require_recorded())
    previous: tuple[TeamUpdate, ...] = (TeamUpdateNotice(lead, (unbound,)),)
    for now in (1000.0, 1060.0):  # the first and second passes after the carry
        packet = TeamUpdate.derive(lead, (), (current,), previous, published=(), now=now,
                                   history=history).packet
        assert {(ping, ping.since) for ping in packet.handled} == {
            (unbound, RecordedEventTime(100.0)),
            (RulePing.capture(current), RecordedEventTime(1000.0))}
        previous = (packet,)
    cleared = TeamUpdate.derive(lead, (), (), previous, published=(), now=1120.0,
                                history=history).packet
    assert cleared.handled == ()
