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
from itertools import dropwhile
from typing import ClassVar

from agent_comms_hooks import ClaudeHook, ClaudeToolHookProgram
from agent_comms_hooks.reread import ToolInput


@dataclass(frozen=True)
class Bash(ToolInput):
    """The Bash tool's input: one shell command line."""

    command: str

    loops: ClassVar[frozenset[str]] = frozenset(("until", "while"))
    openers: ClassVar[frozenset[str]] = frozenset(("do", "then", "!", "{"))
    """Words after which the next word is again a command."""
    shell: ClassVar[re.Pattern[str]] = re.compile(r"(?:ba)?sh")
    script_flag: ClassVar[re.Pattern[str]] = re.compile(r"-\w*c")
    seconds: ClassVar[re.Pattern[str]] = re.compile(r"(\d+)s?")
    pgrep_f: ClassVar[re.Pattern[str]] = re.compile(r"\bpgrep\s+(?:-\w*\s+)*-\w*f")
    max_seconds: ClassVar[int] = 3600

    # The shell's quoting, declared once: a single-quoted run, a double-quoted run (where a
    # backslash escapes), one escaped character, or one plain character. An unclosed quote
    # runs to the end of the command, as it does while the shell reads on.
    piece: ClassVar[str] = r"""'(?:[^']*)'?|"(?:\\.|[^"\\])*"?|\\.|[^'"\\{plain}]"""
    segment: ClassVar[re.Pattern[str]] = re.compile(f"(?:{piece.format(plain=';&|\n()')})+", re.S)
    """Text between top-level separators; quoted or escaped separators stay inside it."""
    word: ClassVar[re.Pattern[str]] = re.compile(f"(?:{piece.format(plain=r'\s')})+", re.S)
    unquote: ClassVar[re.Pattern[str]] = re.compile(r"""'([^']*)'?|"((?:\\.|[^"\\])*)"?|\\(.)""", re.S)

    def segments(self) -> tuple[tuple[str, ...], ...]:
        """The command's unquoted words, split at its top-level separators."""
        return tuple(
            words for text in self.segment.findall(self.command)
            if (words := tuple(
                self.unquote.sub(lambda quoted: "".join(filter(None, quoted.groups())), word)
                for word in self.word.findall(text)
            ))
        )

    def holds_loop(self) -> bool:
        return any(map(self.runs_loop, self.segments()))

    def runs_loop(self, segment: tuple[str, ...]) -> bool:
        """A loop keyword at command position, or in the script a shell runs with -c."""
        words = tuple(dropwhile(self.openers.__contains__, segment))
        if words[:1] == ("timeout",):
            words = words[2:]
        match words:
            case (keyword, *_) if keyword in self.loops:
                return True
            case (program, flag, script, *_) if self.shell.fullmatch(program) and self.script_flag.fullmatch(flag):
                return Bash(script).holds_loop()
        return False

    def bounded(self, segment: tuple[str, ...]) -> bool:
        match segment:
            case ("timeout", limit, *_) if bound := self.seconds.fullmatch(limit):
                return int(bound.group(1)) <= self.max_seconds
        return False

    def unbounded_wait(self) -> tuple[str, ...]:
        """Why this command's wait loop could run forever; empty when it holds no loop."""
        waits = tuple(filter(self.runs_loop, self.segments()))
        if not waits:
            return ()
        reasons = []
        if self.pgrep_f.search(self.command):
            reasons.append(
                "a wait loop may not use `pgrep -f`: it matches its own command line and the "
                "other waiters; wait on a PID (`while kill -0 PID`), a file, or use "
                "run_in_background, which notifies when the command exits"
            )
        if not all(map(self.bounded, waits)):
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
