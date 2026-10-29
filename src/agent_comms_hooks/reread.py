"""Completed tool calls are judged and reminded by the resident session owner."""

from .watch import SessionHookProgram


class ReminderProgram(SessionHookProgram):
    events = ("PostToolUse",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{__spec__.name}:main")


main = ReminderProgram.main

if __name__ == "__main__":
    main()
