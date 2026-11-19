import json

import pytest
from cutover.carry_shared_team_frames import FlatTeamFrameFile, SharedTeamFrames

from agent_comms.nominal.field_codec import FieldCodec, RecordProblems
from agent_comms.planning.plan_phases import ReviewQueuePhase
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.storage.locked_store import LiveFilesRequest
from agent_comms.team.team_hud import (
    CodexUsage,
    TeamBoardPublicationStore,
    TeamHud,
    TeamPlan,
    UnreadableUsage,
)
from agent_comms.team.team_hud_records import TeamPhase
from agent_comms.team.vibe_check import OverloadDegradation


def test_a_frame_written_with_its_own_phases_moves_them_to_the_team_plan(tmp_path):
    owner = RecordedThreadIncarnation("colleague", 3.0)
    phases = (TeamPhase(ReviewQueuePhase, False, True, ()),)
    hud = TeamHud(
        teammate=owner, units=(), load=OverloadDegradation.from_orders((), 0, 0), forks=(),
        usage=CodexUsage(UnreadableUsage(reason="not requested")), packets=(), open_pings=(),
        phases=phases,
    )
    frame = FlatTeamFrameFile.path_in(tmp_path, lookup=str(owner.lookup))
    frame.parent.mkdir(parents=True)
    encoded = FieldCodec.encode(hud, TeamHud)
    encoded["phases"] = FieldCodec.encode(TeamPlan(phases), TeamPlan)["phases"]
    frame.write_text(json.dumps(encoded))
    step = SharedTeamFrames(LiveFilesRequest(tmp_path), tmp_path / "spill")
    assert step.carry(apply=False).changed == 1
    step.carry(apply=True)
    publication = TeamBoardPublicationStore.at(tmp_path).read()
    restored, = TeamBoardPublicationStore.read_all(tmp_path)
    assert publication.plan.phases == phases
    assert restored.phases == phases
    assert not frame.exists()
