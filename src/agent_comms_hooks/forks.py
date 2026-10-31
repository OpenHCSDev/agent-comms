"""Subagent events use the parent's existing resident session owner."""

from .watch import SessionHookProgram


class SubagentStartProgram(SessionHookProgram):
    events = ("SubagentStart",)


class SubagentStopProgram(SessionHookProgram):
    events = ("SubagentStop",)
