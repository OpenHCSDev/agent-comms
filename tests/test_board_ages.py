"""One team board owns each line's age: every teammate's retained packet dates it."""

from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.heartbeat_subjects import TeamHistory
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
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


def board(tmp_path):
    comms = wire(tmp_path)
    for name, born in (("lead", 1.0), ("peer", 2.0)):
        comms.registry.declare(Thread(name, frozenset(), Checkout(tmp_path), created_at=born))
    return comms, *(comms.registry.require(name).incarnation for name in ("lead", "peer"))


@pytest.mark.parametrize("start", [RecordedEventTime(100.0), UnrecordedEventTime()],
                         ids=["dated", "undated"])
def test_an_unbound_line_is_an_observation_kept_through_every_pass(tmp_path, start):
    """The first and every later pass keep the pre-cutover line with its original start,
    recorded or not, beside the capture it cannot match. A pass with no line of its rule does
    not clear it: the thread it watched may still be deaf, just no longer captured."""
    comms, lead, peer = board(tmp_path)
    history = TeamHistory.read(comms, frozenset())
    unbound = UnboundRulePing(TeamDeaf, TextDigest("0" * 64), text="old deaf", since=start)
    current = TeamDeaf(900.0, peer.require_recorded())
    previous: tuple[TeamUpdate, ...] = (TeamUpdateNotice(lead, (unbound,)),)
    for now, rules in ((1000.0, (current,)), (1060.0, (current,)), (1120.0, ())):
        packet = TeamUpdate.derive(lead, (), rules, previous, published=(), now=now,
                                   history=history).packet
        kept, = (ping for ping in packet.handled if ping == unbound)
        assert kept.since == start
        assert not kept.on_board(history) and not kept.waits_on_team(now)
        previous = (packet,)
