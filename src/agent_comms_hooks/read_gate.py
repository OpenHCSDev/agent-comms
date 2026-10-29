"""PreToolUse is a lightweight request to the existing resident session owner."""

from . import PermissionHookProgram
from .watch import SessionHookProgram


class ReadGateProgram(SessionHookProgram, PermissionHookProgram):
    events = ("PreToolUse",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{__spec__.name}:main")


main = ReadGateProgram.main

if __name__ == "__main__":
    main()
