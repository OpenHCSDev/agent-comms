"""SessionEnd uses the existing resident owner without importing Core."""

from .watch import SessionHookProgram


class SessionEnded(SessionHookProgram):
    events = ("SessionEnd",)
