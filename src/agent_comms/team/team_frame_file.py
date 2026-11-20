from __future__ import annotations

from pathlib import Path

from agent_comms.storage.locked_store import PatternFile, RuntimeFile


class TeamBoardFile(RuntimeFile, PatternFile):
    """The heartbeat's one atomic board publication file."""

    __slots__ = ()

    filename = "team_frames.json"

    @classmethod
    def source_paths(cls, root: Path) -> tuple[Path, ...]:
        return (cls.path_in(root),)
