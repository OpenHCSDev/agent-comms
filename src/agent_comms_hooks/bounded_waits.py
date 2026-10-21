"""A Bash wait loop must end on its own: it starts with `timeout N`, and its condition never
uses `pgrep -f`, whose pattern also matches the waiter's own command line and every other
waiter naming the same task, so such loops keep each other alive forever.

On 2026-10-21 the commander session and its forks left 177 of these loops running (the
oldest 2,981 minutes), 378 forks/s and 48% system CPU on a 12-core machine.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import ClassVar

from agent_comms_hooks import ClaudeHook, ClaudeToolHookProgram
from agent_comms_hooks.reread import ToolInput


@dataclass(frozen=True)
class Bash(ToolInput):
    """The Bash tool's input: one shell command line."""

    command: str

    loop: ClassVar[re.Pattern[str]] = re.compile(r"(?:^|[\s;&|('\"`])(?:until|while)\s")
    bounded: ClassVar[re.Pattern[str]] = re.compile(r"\A\s*timeout\s+(\d+)s?\s")
    pgrep_f: ClassVar[re.Pattern[str]] = re.compile(r"\bpgrep\s+(?:-\w*\s+)*-\w*f")
    separators: ClassVar[str] = ";&|\n"
    max_seconds: ClassVar[int] = 3600

    def segments(self) -> tuple[str, ...]:
        """The command split at its top-level separators; a quoted separator stays inside."""
        segments, current, quote, escaped = [], [], "", False
        for char in self.command:
            if escaped:
                escaped = False
            elif char == "\\" and quote != "'":
                escaped = True
            elif quote:
                quote = "" if char == quote else quote
            elif char in "'\"":
                quote = char
            elif char in self.separators:
                segments.append("".join(current))
                current = []
                continue
            current.append(char)
        return (*segments, "".join(current))

    def bounded_segment(self, segment: str) -> bool:
        bound = self.bounded.match(segment)
        return bool(bound) and int(bound.group(1)) <= self.max_seconds

    def unbounded_wait(self) -> tuple[str, ...]:
        """Why this command's wait loop could run forever; empty when it holds no loop."""
        if not self.loop.search(self.command):
            return ()
        reasons = []
        if self.pgrep_f.search(self.command):
            reasons.append(
                "a wait loop may not use `pgrep -f`: it matches its own command line and the "
                "other waiters; wait on a PID (`while kill -0 PID`), a file, or use "
                "run_in_background, which notifies when the command exits"
            )
        if not all(
            self.bounded_segment(segment)
            for segment in self.segments()
            if self.loop.search(segment)
        ):
            reasons.append(
                f"a wait loop must start with `timeout N` (N <= {self.max_seconds}) so it dies "
                "on its own; for a long watch use the Monitor tool, which is capped"
            )
        return tuple(reasons)


@dataclass(frozen=True)
class PreToolUseHook(ClaudeHook):
    tool_name: str
    tool_input: dict

    def deny(self, reason: str) -> None:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": self.hook_event_name,
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )


class BoundedWaits(ClaudeToolHookProgram):
    events = ("PreToolUse",)
    tool = Bash.__name__

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    hook = PreToolUseHook.read()
    reasons = Bash.decode(hook.tool_input).unbounded_wait()
    if reasons:
        hook.deny("Unbounded wait refused: " + "; ".join(reasons))


if __name__ == "__main__":
    main()
