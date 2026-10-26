"""Required checks and the commander's head verdict protect merges.

The owner's admin bypass stays. A commander-session admin or API merge needs
Core's recorded ok verdict on the current head. Other hook sessions cannot
bypass the checks. claude-ai's separate owner path is unchanged.

Ordinary Bash calls stay standard-library-only; only a merge invokes Core's reader.
"""

from __future__ import annotations

import getopt
import re
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import ClassVar

from agent_comms.shell_commands import Program, ShellCommand
from agent_comms_hooks import Bash, BashRefusal, ClaudeToolHook, HookRecord, HookRefused
from agent_comms_hooks.watch import WatchClaim

GH = Program("gh")


@dataclass(frozen=True)
class MergeTarget(HookRecord):
    """The merge's repository and PR, decoded once at the command boundary."""

    repository: str
    number: int


@dataclass(frozen=True)
class QueueBypass(ABC):
    """One external merge command and its decoded merge target."""

    command: ShellCommand
    reason: ClassVar[str]

    @classmethod
    @abstractmethod
    def merges(cls, args: list[str]) -> bool: ...

    @abstractmethod
    def target(self) -> MergeTarget: ...

    @classmethod
    def matching(cls, command: ShellCommand) -> tuple[QueueBypass, ...]:
        if not GH.runs(command):
            return ()
        return tuple(member(command) for member in cls.__subclasses__()
                     if member.merges(command.args))

    @classmethod
    def found(cls, command: ShellCommand) -> tuple[str, ...]:
        return tuple(member.reason for member in cls.matching(command))


class AdminMerge(QueueBypass):
    """The external gh merge options select the PR, never its verdict."""

    reason = "`gh pr merge --admin` bypasses required checks."
    # gh's external option contract: values must not become a PR selector.
    short_options: ClassVar[str] = "R:b:F:dmrst:"
    long_options: ClassVar[tuple[str, ...]] = (
        "repo=", "body=", "body-file=", "delete-branch", "merge", "rebase", "squash",
        "subject=", "admin", "auto", "disable-auto", "author-email=", "match-head-commit=",
    )

    @classmethod
    def merges(cls, args: list[str]) -> bool:
        return args[:2] == ["pr", "merge"] and "--admin" in args

    def target(self) -> MergeTarget:
        options, selectors = getopt.gnu_getopt(
            self.command.args[2:], self.short_options, self.long_options)
        repositories = tuple(value for flag, value in options if flag in ("-R", "--repo"))
        if (len(selectors) != 1 or not selectors[0].isdigit()
                or len(repositories) != 1 or not repositories[0]):
            raise ValueError("An admin merge must name one PR number and --repo OWNER/REPO.")
        return MergeTarget(repository=repositories[0], number=int(selectors[0]))


class ApiMerge(QueueBypass):
    """The merge API's external path already identifies the PR and repository."""

    reason = "`gh api .../pulls/N/merge` bypasses required checks."
    path: ClassVar[re.Pattern[str]] = re.compile(
        r"/?repos/(?P<repository>[^/\s]+/[^/\s]+)/pulls/(?P<number>\d+)/merge")

    @classmethod
    def merges(cls, args: list[str]) -> bool:
        return args[:1] == ["api"] and any(map(cls.path.fullmatch, args))

    def target(self) -> MergeTarget:
        matched = next(match for arg in self.command.args if (match := self.path.fullmatch(arg)))
        return MergeTarget.decode(matched.groupdict())


class QueueBypassGuard(BashRefusal):
    headline = "Merge bypass refused"
    advice = (
        ". Use `gh pr merge N --auto --merge` for required checks and the commander's head verdict."
    )

    @classmethod
    def reasons(cls, command: str) -> tuple[str, ...]:
        return tuple(reason for simple in ShellCommand.split(command)
                     for reason in QueueBypass.found(simple))

    @classmethod
    def reasons_for(cls, hook: ClaudeToolHook) -> tuple[str, ...]:
        command = Bash.decode(hook.tool_input).command
        reasons = cls.reasons(command)
        if not reasons or not hook.parts.exists():
            return reasons
        bindings = WatchClaim(hook.session_id).state().bindings()
        if not bindings:
            return ("The commander session has no recorded bus binding; no verdict can be read.",)
        (entry,) = entry_points(group="console_scripts", name="agent-comms")
        for binding in bindings:
            try:
                checked = subprocess.run(
                    (sys.executable, "-m", entry.module, "--root", binding.root,
                     "merge-review", "--session", hook.session_id, "--bash", command),
                    capture_output=True, text=True, timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise HookRefused(f"Core's merge-verdict reader failed: {error}") from error
            if checked.returncode:
                return (checked.stderr.strip() or checked.stdout.strip(),)
        return ()


main = QueueBypassGuard.main


if __name__ == "__main__":
    main()
