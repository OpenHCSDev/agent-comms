"""Completed tool calls are judged and reminded by the resident session owner.

The session start owner orders the sources read once and deals their interleaved
slices beside the transcript. Each completed tool call supplies its existing
ordinal to the resident reminder; this front end neither recounts nor parses history.
The commander's same reply carries RE-ARM while its bridge lacks a Monitor and
the board changes derived by that bridge from Core.
"""

from .watch import SessionHookProgram


class ReminderProgram(SessionHookProgram):
    events = ("PostToolUse",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{__spec__.name}:main")


main = ReminderProgram.main

if __name__ == "__main__":
    main()
