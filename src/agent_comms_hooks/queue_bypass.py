"""Required checks and the commander's head verdict protect merges.

The owner's admin bypass stays. A commander-session admin or API merge needs
Core's recorded ok verdict on the explicitly pinned head. Other hook sessions cannot
bypass the checks. claude-ai's separate owner path is unchanged.

Ordinary Bash calls stay standard-library-only; only a merge invokes Core's reader.
"""

from __future__ import annotations

import re
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import ClassVar
from urllib.parse import unquote, urlsplit

from agent_comms.shell_commands import Program, ShellCommand
from agent_comms_hooks import Bash, BashRefusal, ClaudeToolHook, HookRecord, HookRefused
from agent_comms_hooks.watch import WatchClaim

GH = Program("gh")


@dataclass(frozen=True)
class MergeTarget(HookRecord):
    """The external merge's repository, PR and required head precondition."""

    repository: str
    number: int
    head: str


@dataclass(frozen=True)
class QueueBypass(ABC):
    """One external merge command and its decoded merge target."""

    command: ShellCommand
    reason: ClassVar[str]
    prefix: ClassVar[tuple[str, ...]]

    @classmethod
    def merges(cls, args: list[str]) -> bool:
        words = iter(args)
        return all(word in words for word in cls.prefix) and cls.selects(args)

    @classmethod
    @abstractmethod
    def selects(cls, args: list[str]) -> bool: ...

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
    """GitHub's head precondition and Core's verdict name the same commit."""

    reason = "`gh pr merge --admin` bypasses required checks."
    prefix = ("pr", "merge")
    @classmethod
    def selects(cls, args: list[str]) -> bool:
        return any(arg.partition("=")[0] == "--admin" for arg in args)

    def target(self) -> MergeTarget:
        options, selectors = self.command.options(self.prefix)
        try:
            repository, = options.get("--repo", ())
            head, = options.get("--match-head-commit", ())
            selector, = selectors
            return MergeTarget(repository=repository, number=int(selector), head=head)
        except ValueError as error:
            raise ValueError("Admin merge requires one PR number, --repo and --match-head-commit HEAD.") from error


class ApiMerge(QueueBypass):
    """The merge endpoint and literal sha body field select the reviewed head."""

    reason = "`gh api .../pulls/N/merge` bypasses required checks."
    prefix = ("api",)
    path: ClassVar[re.Pattern[str]] = re.compile(
        r"\A/?repos/(?P<repository>[^/\s]+/[^/\s]+)/pulls/(?P<number>\d+)/merge\Z")

    @classmethod
    def endpoint(cls, value: str) -> tuple[re.Match[str], ...]:
        return tuple(cls.path.finditer(unquote(urlsplit(value).path).rstrip("/")))

    @classmethod
    def selects(cls, args: list[str]) -> bool:
        return any(map(cls.endpoint, args))

    def target(self) -> MergeTarget:
        options, selectors = self.command.options(self.prefix)
        if options.get("--input", ()):
            raise ValueError("A merge bypass requires a literal sha body field, not --input.")
        selector, = selectors
        try:
            matched, = self.endpoint(selector)
        except ValueError as error:
            raise ValueError("A merge bypass requires one explicit repos/OWNER/REPO/pulls/N/merge endpoint.") from error
        fields: dict[str, list[str]] = {}
        for field in (*options.get("--raw-field", ()), *options.get("--field", ())):
            name, _, value = field.partition("=")
            fields.setdefault(name, []).append(value)
        try:
            head, = fields.get("sha", ())
        except ValueError as error:
            raise ValueError("A merge bypass requires exactly one literal sha=HEAD body field.") from error
        return MergeTarget.decode({**matched.groupdict(), "head": head})


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
        try:
            reasons = cls.reasons(command)
        except ValueError as error:
            raise HookRefused(f"Cannot read merge command: {error}") from error
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
