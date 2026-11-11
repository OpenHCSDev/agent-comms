from __future__ import annotations

from pathlib import Path

from agent_comms.coordination.service_passes import ServicePassStore
from agent_comms.storage.locked_store import PatternFile, RuntimeFile


class TeamPlanFile(RuntimeFile):
    """Where the heartbeat writes the team's plan phases, once per beat for every teammate.

    Phase readiness is a team fact: every frame reads it here at read time, and a process
    with no frame of its own (an order sent from the CLI) reads it without counting main.
    """

    __slots__ = ()

    filename = "team_plan.json"


class TeamFrameFile(RuntimeFile, PatternFile):
    """Where the heartbeat writes each teammate's newest HUD frame.

    Views watch these paths below Comms; the HUD that decodes them sits above it.
    """

    __slots__ = ()

    filename = "team_frames/{lookup}.json"

    @classmethod
    def source_paths(cls, root: Path) -> tuple[Path, ...]:
        """Frame readers hydrate service health and the team plan too; each file wakes
        their open view."""
        return (*sorted(cls.matching_in(root)), ServicePassStore.path_in(root),
                TeamPlanFile.path_in(root))
