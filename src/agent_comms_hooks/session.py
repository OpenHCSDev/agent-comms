"""SessionStart bootstraps Core; later lifecycle events use its resident owner."""
import subprocess
import sys

from agent_comms_hooks import ClaudeHookProgram

from .watch import SessionHookProgram


class SessionStarted(ClaudeHookProgram):
    events = ("SessionStart",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{__spec__.name}:started")


class SessionEnded(SessionHookProgram):
    events = ("SessionEnd",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{__spec__.name}:ended")


def started() -> None:
    """This one startup process is allowed to load the Core that starts the bridge."""
    raise SystemExit(subprocess.call((sys.executable, "-m", "agent_comms.cli_bridges.hook_session")))


ended = SessionEnded.main
