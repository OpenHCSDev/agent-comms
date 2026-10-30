"""Completed tool calls are judged and reminded by the resident session owner."""
from .watch import SessionHookProgram


class ReminderProgram(SessionHookProgram):
    events = ("PostToolUse",)


if __name__ == "__main__":
    ReminderProgram.main()
