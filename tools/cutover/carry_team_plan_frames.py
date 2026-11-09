"""Move the plan phases stored in each HUD frame into the one team plan file.

Phase readiness is a team fact the heartbeat now writes once, to ``team_plan.json``. Frames
written before that still hold their own copy under ``phases``, which the frame decoder
refuses as an unknown field, so every board read would fail until the first beat rewrote
them. The first frame's copy becomes the team plan, every frame's copy is dropped, and the
first read after installation shows the same phases as before. Delete this step after the
live installation.
"""

from __future__ import annotations

import json
from typing import ClassVar

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.storage.locked_store import RootFile
from agent_comms.storage.store_files import AtomicFile
from agent_comms.team.team_frame_file import TeamFrameFile, TeamPlanFile


class TeamPlanFrames(Carry, declared_name="team_plan_frames"):
    """Each frame's stored ``phases`` moves out; the team plan file holds them once."""

    stored: ClassVar[str] = "phases"

    @property
    def opened_files(self) -> tuple[type[RootFile], ...]:
        return (TeamFrameFile, TeamPlanFile)

    def carry(self, *, apply: bool) -> Carried:
        frames = {
            path: document
            for path in sorted(TeamFrameFile.matching_in(self.root))
            if self.stored in (document := json.loads(path.read_text(encoding="utf-8")))
        }
        # One board writes every frame of a pass, so each frame holds the same phases.
        plans = {TeamPlanFile.path_in(self.root): {self.stored: document[self.stored]}
                 for document in frames.values()}
        if apply:
            for path, plan in plans.items():
                AtomicFile(path).write_text(json.dumps(plan))
            for path, document in frames.items():
                AtomicFile(path).write_text(json.dumps(
                    {key: value for key, value in document.items() if key != self.stored}))
        return Carried(
            len(frames),
            rewritten=tuple(path.relative_to(self.root) for path in (*frames, *plans))
            if apply else (),
        )
