from __future__ import annotations

from pathlib import Path

from agent_comms.service_passes import ServicePassStore
from agent_comms.locked_store import PatternFile, RuntimeFile


class CommanderFrameFile(RuntimeFile, PatternFile):
    """Where the heartbeat writes each commander's newest HUD frame.

    Views watch these paths below Comms; the HUD that decodes them sits above it.
    """

    __slots__ = ()

    filename = "commander_frames/{lookup}.json"

    @classmethod
    def source_paths(cls, root: Path) -> tuple[Path, ...]:
        """Frame readers hydrate service health too; both files wake their open view."""
        return (*sorted(cls.matching_in(root)), ServicePassStore.path_in(root))
