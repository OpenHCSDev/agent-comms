"""SessionStart alone loads Core; its console script runs the actual startup owner."""
from agent_comms.cli_bridges.hook_session import main

from . import ClaudeHookProgram


class SessionStarted(ClaudeHookProgram):
    events = ("SessionStart",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")
