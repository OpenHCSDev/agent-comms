"""Required checks and the commander's head verdict protect merges.

The owner's admin bypass stays, and so does a session acting on the owner's own
prompt: when the newest prompt in the transcript is one he typed, he is standing behind
the command and it runs. A commander-session admin or API merge without him needs
Core's recorded ok verdict on the explicitly pinned head. Other hook sessions cannot
bypass the checks. claude-ai's separate owner path is unchanged.

Ordinary Bash calls stay standard-library-only; only a merge invokes Core's reader.
"""

from __future__ import annotations

import getopt
import re
import subprocess
import sys
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import ClassVar, Self
from urllib.parse import unquote, urlsplit

from agent_comms.cli_bridges.claude_transcript import ClaudeJsonLines
from agent_comms.errors import ToolRefusedError
from agent_comms.shell_commands import Program, ShellCommand
from agent_comms_hooks import Bash, BashRefusal, ClaudeToolHook, HookRecord, HookRefused
from agent_comms_hooks.watch import WatchClaim


class MergeTargetUnreadable(HookRefused, ToolRefusedError):
    """A merge command that does not name exactly one reviewed head."""


class Gh(Program):
    """GitHub's executable owns command selection, option aliases and arity."""

    def options(self, command: ShellCommand) -> tuple[Parameters, list[str]]:
        try:
            help_text = subprocess.run(
                (command.words[0], "help", "--", *command.args), check=True,
                capture_output=True, text=True, timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise MergeTargetUnreadable(f"{self.name} could not declare its options: {error}") from error
        short, long = [], []
        aliases: dict[str, str] = {}
        for declaration in re.finditer(
            r"^  (?:-(\S), |    )?--([\w-]+)(?: ([^\s]+))? {2,}", help_text, re.MULTILINE,
        ):
            alias, name, argument = declaration.groups()
            long.append(name + ("=" if argument else ""))
            if alias:
                short.append(alias + (":" if argument else ""))
                aliases["-" + alias] = "--" + name
        if not long:
            raise MergeTargetUnreadable(f"{self.name} declared no readable command options.")
        try:
            options, selectors = getopt.gnu_getopt(command.args, "".join(short), long)
        except getopt.GetoptError as error:
            raise MergeTargetUnreadable(f"Unsupported {self.name} command option: {error}") from error
        values = Parameters()
        for flag, value in options:
            values[aliases.get(flag, flag)].append(value)
        return values, selectors


class Parameters(defaultdict[str, list[str]]):
    """Named command values, preserving duplicates and owning required-value cardinality."""

    def __init__(self) -> None:
        super().__init__(list)

    def one(self, name: str) -> str:
        if len(self[name]) != 1:
            raise MergeTargetUnreadable(f"A merge bypass requires exactly one {name} value.")
        return self[name][0]

    def fields(self, *names: str) -> Self:
        parameters = type(self)()
        for name in names:
            for field in self[name]:
                key, _, value = field.partition("=")
                parameters[key].append(value)
        return parameters


GH = Gh("gh")


@dataclass(frozen=True)
class MergeTarget(HookRecord):
    """The external merge's repository, PR and required head precondition."""

    repository: str
    number: int
    head: str


@dataclass(frozen=True)
class QueueBypass(ABC):
    """One external merge command and its decoded merge target."""

    options: Parameters
    selectors: tuple[str, ...]
    reason: ClassVar[str]
    prefix: ClassVar[tuple[str, ...]]
    repo_flag: ClassVar[str] = "--repo"

    @classmethod
    def possible(cls, args: list[str]) -> bool:
        words = iter(args)
        return all(word in words for word in cls.prefix) and cls.mentions(args)

    @classmethod
    def mentions(cls, args: list[str]) -> bool:
        return True

    @classmethod
    @abstractmethod
    def selected(cls, options: Parameters, selectors: tuple[str, ...]) -> bool: ...

    @abstractmethod
    def target(self) -> MergeTarget: ...

    @classmethod
    def matching(cls, command: ShellCommand) -> tuple[QueueBypass, ...]:
        if not GH.runs(command):
            return ()
        candidates = tuple(member for member in cls.__subclasses__()
                           if member.possible(command.args))
        if not candidates:
            return ()
        options, selectors = GH.options(command)
        return tuple(member(options, tuple(selectors[len(member.prefix):]))
                     for member in candidates
                     if tuple(selectors[:len(member.prefix)]) == member.prefix
                     and member.selected(options, tuple(selectors[len(member.prefix):])))

    @classmethod
    def found(cls, command: ShellCommand) -> tuple[str, ...]:
        return tuple(member.reason for member in cls.matching(command))


class AdminMerge(QueueBypass):
    """GitHub's head precondition and Core's verdict name the same commit."""

    reason = "`gh pr merge --admin` bypasses required checks."
    prefix = ("pr", "merge")
    admin_flag: ClassVar[str] = "--admin"
    head_flag: ClassVar[str] = "--match-head-commit"

    @classmethod
    def mentions(cls, args: list[str]) -> bool:
        return any(arg.partition("=")[0] == cls.admin_flag for arg in args)

    @classmethod
    def selected(cls, options: Parameters, selectors: tuple[str, ...]) -> bool:
        return cls.admin_flag in options

    def target(self) -> MergeTarget:
        self.options.one(self.admin_flag)
        if len(self.selectors) != 1 or not self.selectors[0].isdecimal():
            raise MergeTargetUnreadable("Admin merge requires one PR number.")
        return MergeTarget(repository=self.options.one(self.repo_flag),
                           number=int(self.selectors[0]), head=self.options.one(self.head_flag))


class ApiMerge(QueueBypass):
    """The merge endpoint and literal sha body field select the reviewed head."""

    reason = "`gh api .../pulls/N/merge` bypasses required checks."
    prefix = ("api",)
    field_flags: ClassVar[tuple[str, ...]] = ("--raw-field", "--field")
    input_flag: ClassVar[str] = "--input"
    path: ClassVar[re.Pattern[str]] = re.compile(
        r"\A/?repos/(?P<repository>[^/\s]+/[^/\s]+)/pulls/(?P<number>\d+)/merge\Z")

    @classmethod
    def endpoint(cls, value: str) -> tuple[re.Match[str], ...]:
        return tuple(cls.path.finditer(unquote(urlsplit(value).path).rstrip("/")))

    @classmethod
    def selected(cls, options: Parameters, selectors: tuple[str, ...]) -> bool:
        return any(map(cls.endpoint, selectors))

    def target(self) -> MergeTarget:
        if self.input_flag in self.options:
            raise MergeTargetUnreadable(
                f"A merge bypass requires a literal sha body field, not {self.input_flag}.")
        endpoints = tuple(match for selector in self.selectors for match in self.endpoint(selector))
        if len(self.selectors) != 1 or len(endpoints) != 1:
            raise MergeTargetUnreadable(
                "A merge bypass requires one explicit repos/OWNER/REPO/pulls/N/merge endpoint.")
        head = self.options.fields(*self.field_flags).one("sha")
        return MergeTarget.decode({**endpoints[0].groupdict(), "head": head})


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
        if reasons and cls.owner_present(hook):
            return ()
        state = WatchClaim(hook.session_id).state()
        if not reasons or not state.commands():
            return reasons
        (entry,) = entry_points(group="console_scripts", name="agent-comms")
        for binding in state.bindings():
            try:
                checked = subprocess.run(
                    (sys.executable, "-m", entry.module, "--root", binding.root,
                     "merge-review", "--session", hook.session_id, "--bash", command),
                    capture_output=True, text=True, timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise HookRefused(f"Core's merge-verdict reader failed: {error}") from error
            if checked.returncode:
                return (checked.stdout.strip(),)
        return ()


    @staticmethod
    def owner_present(hook: ClaudeToolHook) -> bool:
        """Whether the person typed the newest prompt in this session's transcript."""
        transcript = Path(hook.transcript_path)
        with transcript.open("rb") as stream:
            return ClaudeJsonLines().owner_present(stream, transcript.stat().st_size)


main = QueueBypassGuard.main


if __name__ == "__main__":
    main()
