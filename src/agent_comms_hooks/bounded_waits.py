"""A Bash wait loop must end on its own: it runs as `timeout N bash -c '...'`, and its
condition never uses `pgrep -f`, whose pattern also matches the waiter's own command line
and every other waiter naming the same task, so such loops keep each other alive forever.

On 2026-10-21 the commander session and its forks left 177 of these loops running (the
oldest 2,981 minutes), 378 forks/s and 48% system CPU on a 12-core machine.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from itertools import dropwhile, takewhile
from typing import ClassVar

from agent_comms_hooks import Bash, BashRefusal
from agent_comms_hooks.shell import ShellCommand, shell_commands

Words = tuple[str, ...]


@dataclass(frozen=True)
class Timeout:
    """`timeout [OPTION]... DURATION COMMAND`: whether it bounds the command it runs, and
    that command. Any other command runs itself, unbounded."""

    program: ClassVar[str] = "timeout"
    valued: ClassVar[frozenset[str]] = frozenset(("-k", "-s", "--kill-after", "--signal"))
    """Options whose value is the next word."""
    duration: ClassVar[re.Pattern[str]] = re.compile(r"(\d+(?:\.\d*)?)([smhd]?)")
    unit: ClassVar[dict[str, int]] = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
    max_seconds: ClassVar[int] = 3600

    bounded: bool
    runs: Words

    @classmethod
    def around(cls, command: ShellCommand) -> Timeout:
        if command.program != cls.program:
            return cls(False, command.words)
        rest = command.words[1:]
        while rest[:1] and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in cls.valued else rest[1:]
        limit = cls.duration.fullmatch(rest[0]) if rest else None
        seconds = float(limit.group(1)) * cls.unit[limit.group(2)] if limit else 0
        return cls(0 < seconds <= cls.max_seconds, rest[1:])

    def commands(self) -> Iterator[ShellCommand]:
        return ShellCommand.of(self.runs)


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
    full: ClassVar[re.Pattern[str]] = re.compile(r"--full|-\w*f\w*")
    """`--full`, or a cluster of short options holding `f`."""

    @classmethod
    def full_match(cls, segment: Words) -> bool:
        return any(
            run.program == cls.program
            and any(map(cls.full.fullmatch, takewhile(lambda arg: arg.startswith("-"), run.args)))
            for run in WaitCommand.commands(segment)
        )


@dataclass(frozen=True)
class WaitCommand(Bash):
    """The Bash tool's input, read for the wait loops it runs."""

    loops: ClassVar[frozenset[str]] = frozenset(("until", "while"))
    body: ClassVar[Words] = ("do",)
    """The word that ends a loop's condition."""
    openers: ClassVar[frozenset[str]] = frozenset((*body, "then", "!", "{"))
    """Words after which the next word is again a command."""
    shell: ClassVar[re.Pattern[str]] = re.compile(r"(?:ba)?sh")
    script_flag: ClassVar[re.Pattern[str]] = re.compile(r"-\w*c")

    @classmethod
    def commands(cls, segment: Words) -> Iterator[ShellCommand]:
        """What one segment runs: its command, or the command its `timeout` runs."""
        for command in ShellCommand.of(dropwhile(cls.openers.__contains__, segment)):
            yield from Timeout.around(command).commands()

    def waits(self) -> Iterator[Wait]:
        """Every loop this command runs, at command position or in a script a shell runs with
        -c. Only such a script can be bounded: `timeout` runs programs, not shell keywords."""
        segments = tuple(map(tuple, shell_commands(self.command)))
        for index, segment in enumerate(segments):
            for command in ShellCommand.of(dropwhile(self.openers.__contains__, segment)):
                timeout = Timeout.around(command)
                for run in timeout.commands():
                    match (run.program, *run.args):
                        case (keyword, *condition) if keyword in self.loops:
                            yield Wait(
                                False,
                                (
                                    tuple(condition),
                                    *takewhile(
                                        lambda later: later[:1] != self.body, segments[index + 1 :]
                                    ),
                                ),
                            )
                        case (program, flag, script, *_) if self.shell.fullmatch(
                            program
                        ) and self.script_flag.fullmatch(flag):
                            for wait in WaitCommand(script).waits():
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
                f"a wait loop must run as `timeout N bash -c '...'` (N <= {Timeout.max_seconds})"
                " so it dies on its own; for a long watch use the Monitor tool, which is capped"
            )
        return tuple(reasons)


class BoundedWaits(BashRefusal):
    headline = "Unbounded wait refused"

    @classmethod
    def reasons(cls, command: str) -> tuple[str, ...]:
        return WaitCommand(command).unbounded_wait()


main = BoundedWaits.main


if __name__ == "__main__":
    main()
