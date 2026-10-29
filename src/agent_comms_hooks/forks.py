"""Subagent events use the parent's existing resident session owner."""

from .watch import SessionHookProgram


class SubagentStartProgram(SessionHookProgram):
    events = ("SubagentStart",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{__spec__.name}:started")


class SubagentStopProgram(SessionHookProgram):
    events = ("SubagentStop",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{__spec__.name}:stopped")


started = SubagentStartProgram.main
stopped = SubagentStopProgram.main
