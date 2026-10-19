"""The commander's send guard, declared here so install can write its hook without importing it."""

from __future__ import annotations

from agent_comms.commander import cli
from agent_comms_hooks import BashInput, ClaudeToolHookProgram


class SendGuard(ClaudeToolHookProgram):
    """Refuses the commander's raw sends and tactical work in the Bash command about to run."""

    events = ("PreToolUse",)
    reads = BashInput
    module = "agent_comms.commander.send_guard"

    @classmethod
    def command(cls) -> str:
        commander = cls.console_script(f"{cli.__name__}:{cli.main.__name__}")
        return f"{commander} {cli.tool_name(cls.module)}"
