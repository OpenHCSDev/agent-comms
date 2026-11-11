import json

import pytest
from cutover.carry_team_plan_frames import TeamPlanFrames

from agent_comms.nominal.field_codec import FieldCodec, RecordProblems
from agent_comms.planning.plan_phases import ReviewQueuePhase
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.storage.locked_store import LiveFilesRequest
from agent_comms.team.team_hud import (
    CodexUsage,
    TeamFrame,
    TeamHud,
    TeamPlan,
    TeamPlanDocument,
    UnreadableUsage,
)
from agent_comms.team.team_hud_records import TeamPhase
from agent_comms.team.vibe_check import OverloadDegradation


def test_a_frame_written_with_its_own_phases_moves_them_to_the_team_plan(tmp_path):
    """A frame from before the team plan stores ``phases``; the frame decoder refuses that field,
    so the board would read nothing until a beat rewrote it. The carry moves the phases into the
    team plan and the first read shows the same board."""
    owner = RecordedThreadIncarnation("colleague", 3.0)
    phases = (TeamPhase(ReviewQueuePhase, False, True, ()),)
    hud = TeamHud(
        teammate=owner, units=(), load=OverloadDegradation.from_orders((), 0, 0), forks=(),
        usage=CodexUsage(UnreadableUsage(reason="not requested")), packets=(), open_pings=(),
        phases=phases,
    )
    TeamFrame.replace_all(tmp_path, TeamPlan(phases), (hud,))
    frame = TeamFrame.at(tmp_path, owner).path
    plan = TeamPlanDocument.at(tmp_path).path
    previous = json.loads(frame.read_text())
    previous["phases"] = FieldCodec.encode(TeamPlan(phases), TeamPlan)["phases"]
    frame.write_text(json.dumps(previous))
    plan.unlink()
    with pytest.raises(RecordProblems, match="phases"):
        TeamFrame.at(tmp_path, owner).read()
    step = TeamPlanFrames(LiveFilesRequest(tmp_path), tmp_path / "spill")
    assert {frame} <= set(step.input_files())

    assert step.carry(apply=False).changed == 1
    assert not plan.exists()
    step.carry(apply=True)

    restored, = TeamFrame.read_all(tmp_path)
    assert restored.phases == phases
    assert "phases" not in json.loads(frame.read_text())
    assert step.carry(apply=False).changed == 0
