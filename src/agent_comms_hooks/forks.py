"""Subagent events use the parent's existing resident session owner."""

from agent_comms_hooks.watch import SessionHookProgram


class SubagentStartProgram(SessionHookProgram):
    events = ("SubagentStart",)


class SubagentStopProgram(SessionHookProgram):
    events = ("SubagentStop",)
