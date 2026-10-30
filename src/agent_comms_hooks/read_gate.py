"""PreToolUse is a lightweight request to the existing resident session owner."""

from . import PermissionHookProgram
from .watch import SessionHookProgram


class ReadGateProgram(SessionHookProgram, PermissionHookProgram):
    events = ("PreToolUse",)


main = ReadGateProgram.main

if __name__ == "__main__":
    main()
