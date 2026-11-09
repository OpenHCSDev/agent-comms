"""SessionEnd uses the existing resident owner without importing Core."""

from agent_comms_hooks.watch import SessionHookProgram


class SessionEnded(SessionHookProgram):
    events = ("SessionEnd",)
