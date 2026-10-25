from __future__ import annotations

from agent_comms.locked_store import PatternFile, RuntimeFile


class CommanderFrameFile(RuntimeFile, PatternFile):
    """Where the heartbeat writes each commander's newest HUD frame.

    Views watch these paths below Comms; the HUD that decodes them sits above it.
    """

    __slots__ = ()

    filename = "commander_frames/{lookup}.json"
