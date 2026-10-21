"""A Bash wait loop must end on its own: it starts with `timeout N`, and its condition never
uses `pgrep -f`, whose pattern also matches the waiter's own command line and every other
waiter naming the same task, so such loops keep each other alive forever.

On 2026-10-21 the commander session and its forks left 177 of these loops running (the
oldest 2,981 minutes), 378 forks/s and 48% system CPU on a 12-core machine.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from itertools import dropwhile, takewhile
from typing import ClassVar

from agent_comms_hooks import ClaudeToolHook, ClaudeToolHookProgram
from agent_comms_hooks.reread import ToolInput
from agent_comms_hooks.shell import shell_commands

Words = tuple[str, ...]


@dataclass(frozen=True)
class Timeout:
    """`timeout [OPTION]... DURATION` in front of a command: whether it bounds the command,
    and the command it runs."""

    word: ClassVar[str] = "timeout"
    valued: ClassVar[frozenset[str]] = frozenset(("-k", "-s", "--kill-after", "--signal"))
    """Options whose value is the next word."""
    duration: ClassVar[re.Pattern[str]] = re.compile(r"(\d+(?:\.\d*)?)([smhd]?)")
    unit: ClassVar[dict[str, int]] = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
    max_seconds: ClassVar[int] = 3600

    bounded: bool
    command: Words

    @classmethod
    def split(cls, words: Words) -> Timeout:
        if words[:1] != (cls.word,):
            return cls(False, words)
        rest = words[1:]
        while rest[:1] and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in cls.valued else rest[1:]
        limit = cls.duration.fullmatch(rest[0]) if rest else None
        seconds = float(limit.group(1)) * cls.unit[limit.group(2)] if limit else 0
        return cls(0 < seconds <= cls.max_seconds, rest[1:])


@dataclass(frozen=True)
class Wait:
    """One `until` or `while` loop: whether a timeout bounds it, and the commands of its
    condition, which run on every pass."""

    bounded: bool
    condition: tuple[Words, ...]

    def polls_pgrep_f(self) -> bool:
        return any(map(Pgrep.full_match, self.condition))


class Pgrep:
    """`pgrep -f`: matching the pattern against whole command lines, the waiter's own included."""

    program: ClassVar[str] = "pgrep"
    full: ClassVar[str] = "--full"

    @classmethod
    def full_match(cls, segment: Words) -> bool:
        match Timeout.split(tuple(dropwhile(Bash.openers.__contains__, segment))).command:
            case (cls.program, *args):
                return any(
                    arg == cls.full or (arg[:1] == "-" and arg[1:2] != "-" and "f" in arg[1:])
                    for arg in takewhile(lambda arg: arg.startswith("-"), args)
                )
        return False


@dataclass(frozen=True)
class Bash(ToolInput):
    """The Bash tool's input: one shell command line."""

    command: str

    loops: ClassVar[frozenset[str]] = frozenset(("until", "while"))
    openers: ClassVar[frozenset[str]] = frozenset(("do", "then", "!", "{"))
    """Words after which the next word is again a command."""
    body: ClassVar[Words] = ("do",)
    """The word that ends a loop's condition."""
    shell: ClassVar[re.Pattern[str]] = re.compile(r"(?:ba)?sh")
    script_flag: ClassVar[re.Pattern[str]] = re.compile(r"-\w*c")

    def waits(self) -> Iterator[Wait]:
        """Every loop this command runs, at command position or in a script a shell runs with -c."""
        segments = tuple(map(tuple, shell_commands(self.command)))
        for index, segment in enumerate(segments):
            timeout = Timeout.split(tuple(dropwhile(self.openers.__contains__, segment)))
            match timeout.command:
                case (keyword, *condition) if keyword in self.loops:
                    yield Wait(timeout.bounded, (tuple(condition), *takewhile(
                        lambda later: later[:1] != self.body, segments[index + 1:]
                    )))
                case (program, flag, script, *_) if (
                    self.shell.fullmatch(program) and self.script_flag.fullmatch(flag)
                ):
                    for wait in Bash(script).waits():
                        yield replace(wait, bounded=wait.bounded or timeout.bounded)

    def unbounded_wait(self) -> tuple[str, ...]:
        """Why this command's wait loop could run forever; empty when it holds no loop."""
        waits = tuple(self.waits())
        reasons = []
        if any(wait.polls_pgrep_f() for wait in waits):
            reasons.append(
                "a wait loop may not use `pgrep -f`: it matches its own command line and the "
                "other waiters; wait on a PID (`while kill -0 PID`), a file, or use "
                "run_in_background, which notifies when the command exits"
            )
        if not all(wait.bounded for wait in waits):
            reasons.append(
                f"a wait loop must start with `timeout N` (N <= {Timeout.max_seconds}) so it dies "
                "on its own; for a long watch use the Monitor tool, which is capped"
            )
        return tuple(reasons)


class BoundedWaits(ClaudeToolHookProgram):
    events = ("PreToolUse",)
    tool = Bash.__name__

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    hook = ClaudeToolHook.read()
    reasons = Bash.decode(hook.tool_input).unbounded_wait()
    if reasons:
        hook.respond(
            permissionDecision="deny",
            permissionDecisionReason="Unbounded wait refused: " + "; ".join(reasons),
        )


if __name__ == "__main__":
    main()
