"""PreToolUse is a lightweight request to the existing resident session owner."""

from collections.abc import Mapping

from agent_comms_hooks import ClaudeHook, PermissionHookProgram
from agent_comms_hooks.watch import SessionHookProgram


class ReadGateProgram(SessionHookProgram, PermissionHookProgram):
    events = ("PreToolUse",)

    @classmethod
    def absent(cls, record: Mapping[str, object], error: BaseException) -> None:
        """The one program on every tool call says what is off while the bridge is down."""
        ClaudeHook.decode(record).warn(str(error))


if __name__ == "__main__":
    ReadGateProgram.main()
