"""The board frame is a view; only an owed team gets enforcement and packets."""
from agent_comms.team.team_service import OwedHeartbeat, UnownedHeartbeat


def test_an_owed_team_is_enforced_and_sent_its_packets():
    sent = []
    OwedHeartbeat().publish(lambda: sent.append("packet"))
    assert OwedHeartbeat().actionable(("rule", "state")) == ("rule", "state")
    assert sent == ["packet"]


def test_a_team_owed_nothing_gets_no_enforcement_and_no_packet():
    """2026-11-02: gating the whole pass on an open request left every HUD 'frame missing' for
    13 hours; now the pass still writes the frame and only these two acts are withheld."""
    sent = []
    UnownedHeartbeat().publish(lambda: sent.append("packet"))
    assert UnownedHeartbeat().actionable(("rule", "state")) == ()
    assert sent == []
