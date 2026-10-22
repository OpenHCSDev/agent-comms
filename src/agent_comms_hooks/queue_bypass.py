"""Only claude-ai lands work around the merge queue.

Every agent acts as the same GitHub account, and that account may bypass the merge-queue
ruleset so claude-ai can land an already diagnosed fix without waiting in the queue. A session
on the fleet's machine never does: a merge that skips the queue is refused here, and work lands
through the queue with its checks and the commander's verdict on the head.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import ClassVar

from agent_comms_hooks import BashRefusal
from agent_comms_hooks.shell import Program, ShellCommand

GH = Program("gh")


class QueueBypass(ABC):
    """One way a `gh` command merges a pull request around the merge queue."""

    @classmethod
    @abstractmethod
    def merges(cls, args: list[str]) -> bool: ...

    @classmethod
    def found(cls, command: ShellCommand) -> tuple[str, ...]:
        if not GH.runs(command):
            return ()
        return tuple(
            member.__doc__ or member.__name__
            for member in cls.__subclasses__()
            if member.merges(command.args)
        )


class AdminMerge(QueueBypass):
    """`gh pr merge --admin` merges past the queue and its required checks."""

    @classmethod
    def merges(cls, args: list[str]) -> bool:
        return args[:2] == ["pr", "merge"] and "--admin" in args


class ApiMerge(QueueBypass):
    """`gh api .../pulls/N/merge` merges directly, outside the queue."""

    path: ClassVar[re.Pattern[str]] = re.compile(r"/?repos/[^/\s]+/[^/\s]+/pulls/\d+/merge")

    @classmethod
    def merges(cls, args: list[str]) -> bool:
        return args[:1] == ["api"] and any(map(cls.path.fullmatch, args))


class QueueBypassGuard(BashRefusal):
    headline = "Merge around the queue refused"
    advice = (
        ". Only claude-ai lands work past the queue. Enqueue it with"
        " `gh pr merge N --auto --merge`; the queue runs its checks and the"
        " commander's head verdict."
    )

    @classmethod
    def reasons(cls, command: str) -> tuple[str, ...]:
        return tuple(
            reason for simple in ShellCommand.split(command) for reason in QueueBypass.found(simple)
        )


main = QueueBypassGuard.main


if __name__ == "__main__":
    main()
