"""SessionStart alone loads Core; its console script runs the actual startup owner."""
from agent_comms.cli_bridges.hook_session import SessionHook

from . import ClaudeHookProgram


class SessionStarted(SessionHook, ClaudeHookProgram):
    events = ("SessionStart",)

    @classmethod
    def entrypoint(cls) -> str:
        return f"{SessionHook.__module__}:{SessionHook.__qualname__}.{SessionHook.main.__name__}"
