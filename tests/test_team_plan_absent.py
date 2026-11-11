"""A bus with no beat yet has no plan, and "no phase is known ready" is the empty plan."""

import pytest

from agent_comms.coordination.service_passes import BoardUnavailable
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.team.team_hud import TeamFrame, TeamPlanDocument


def test_a_missing_team_plan_reads_as_no_ready_phase(tmp_path):
    """The plan is the heartbeat's publication of readiness. Before the first beat nobody has
    counted main, so nothing is ready; refusing here stopped every order verb on a bus whose
    heartbeat had never run, which is the bus that needs an order most."""
    plan = TeamPlanDocument.at(tmp_path).read()
    assert plan.phases == ()
    assert plan.ready() == ()


def test_a_missing_frame_still_refuses_the_board_display(tmp_path):
    """A teammate's frame has no empty value: a card that says nothing is not a card."""
    owner = RecordedThreadIncarnation("colleague", 3.0)
    with pytest.raises(BoardUnavailable, match="Tristan frame missing"):
        TeamFrame.at(tmp_path, owner).read()
