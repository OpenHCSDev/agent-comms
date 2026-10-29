"""Programs Claude Code runs as hooks, and what each declares about itself.

Claude Code runs a hook on every prompt or tool call it is registered for, and
importing agent_comms takes a second, so a hook's import path is the standard
library only. Each program declares the events it answers; the command that
runs it is derived from the installed console scripts. Install writes Claude
Code's settings from these declarations.
"""

from __future__ import annotations

import json
import pkgutil
import sys
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from functools import cache
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import ClassVar, NoReturn, Self, get_type_hints


class ToolRefusedError(ValueError):
    """An intentional refusal safe to return as a tool answer, not a transport failure."""


@dataclass(frozen=True)
class HookRecord:
    """A JSON object Claude Code writes to a hook, decoded once by its declared fields."""

    @classmethod
    def decode(cls, record: Mapping[str, object], **context: object) -> Self:
        """A field Claude Code may omit declares its default; a required field it omits is a
        missing argument, refused by the constructor."""
        hints = get_type_hints(cls)
        return cls(
            **{
                declared.name: HookRecord.field(hints[declared.name], record[declared.name])
                for declared in fields(cls)
                if declared.init and not declared.kw_only and declared.name in record
            },
            **context,
        )

    @staticmethod
    def field(declared: type, value: object) -> object:
        """A nested record is decoded by its own fields; any other JSON value is built by its
        declared type, so a nominal identity such as `SubagentId` is one from decode on."""
        if isinstance(value, Mapping) and issubclass(declared, HookRecord):
            return declared.decode(value)
        return declared(value)


class SubagentId(str):
    """Claude Code's name for one subagent, kept across its resumes; Core knows the subagent
    as the commander fork this id names."""

    @property
    def fork(self) -> str:
        return f"claude-{self}"


class HookOutput:
    """A hook's explicit output target; the process boundary writes its own stdout."""

    def line(self, text: str) -> None:
        print(text)


@dataclass(frozen=True)
class ClaudeHook(HookRecord):
    """What Claude Code writes to every hook's stdin."""

    session_id: str
    hook_event_name: str
    output: HookOutput = field(default=HookOutput(), kw_only=True, compare=False, repr=False)

    @classmethod
    def of(cls, record: Mapping[str, object], **context: object) -> ClaudeHook:
        """The event's declaration owns any specialized boundary selection."""
        return cls.decode(record, **context)

    @classmethod
    def read(cls) -> Self:
        return cls.decode(json.load(sys.stdin))

    def respond(self, **fields: str) -> None:
        """Claude Code's answer to this event: its event name plus the given fields."""
        self.output.line(json.dumps({"hookSpecificOutput": {"hookEventName": self.hook_event_name, **fields}}))

    def warn(self, *texts: str) -> None:
        """One event has one JSON answer, however many sources supply its context."""
        if texts:
            self.respond(additionalContext="\n\n".join(texts))


@dataclass(frozen=True)
class ClaudeTranscriptHook(ClaudeHook):
    """A hook that reads the session's transcript, which Claude Code names on every event."""

    transcript_path: str
    window: ClassVar[int] = 1_000_000
    """Transcript bytes read from the end."""

    @property
    def dealt_files(self) -> Path:
        """The packaged rule files dealt to a bound session, one path per line, written by the
        session start hook beside the transcript; it exists only in a session bound to a bus.
        It names the files themselves: a copy of a rule is a paraphrase the session reads
        instead of the rule."""
        return Path(self.transcript_path).with_suffix(".dealt")

    @property
    def dealt_read(self) -> Path:
        """The dealt files read in full since the last deal, one path per line."""
        return Path(self.transcript_path).with_suffix(".dealt-read")

    @property
    def audit(self) -> Path:
        """The session's audit of the dealt rules and the owner's words, written after reading."""
        return Path(self.transcript_path).with_suffix(".audit.json")

    @property
    def audit_due(self) -> Path:
        """What the audit must answer: every dealt rule and each owner prompt shown."""
        return Path(self.transcript_path).with_suffix(".audit-due.json")

    def tail(self) -> list[bytes]:
        """Transcript lines in the last `window` bytes, oldest first; a line the window cuts is dropped."""
        with open(self.transcript_path, "rb") as transcript:
            start = max(0, transcript.seek(0, 2) - self.window)
            transcript.seek(start)
            return transcript.read().splitlines()[1 if start else 0 :]


@dataclass(frozen=True)
class ClaudeToolHook(ClaudeTranscriptHook):
    """What Claude Code adds for a tool-call event: the tool and its input."""

    tool_name: str
    tool_input: dict


class ClaudeHookProgram(ABC):
    """A program Claude Code runs on each of `events`, whatever tool is involved."""

    events: ClassVar[tuple[str, ...]]
    writer: ClassVar[str] = "agent-comms"

    @classmethod
    def members(cls) -> tuple[type[ClaudeHookProgram], ...]:
        """Every program declared in this package."""
        for module in pkgutil.iter_modules(__path__):
            import_module(f"{__name__}.{module.name}")
        return tuple(
            installed
            for program in ClaudeHookProgram.descendants()
            if not program.__abstractmethods__
            for installed in program.installation_members()
        )

    @classmethod
    def installation_members(cls) -> tuple[type[ClaudeHookProgram], ...]:
        """A public program installs itself; a resident handler does not install a copy."""
        return (cls,)

    @classmethod
    def descendants(cls) -> tuple[type[ClaudeHookProgram], ...]:
        # Multiple capabilities reach the same declaration through several paths, not several programs.
        descendants = (member for child in cls.__subclasses__() for member in child.descendants())
        return tuple(dict.fromkeys((cls, *descendants)))

    @classmethod
    @abstractmethod
    def command(cls) -> str:
        """The installed command line that runs this program."""

    @staticmethod
    def console_script(target: str) -> str:
        """The console script installed for a `module:function` target."""
        scripts = entry_points(group="console_scripts")
        (name,) = {point.name for point in scripts if point.value == target}
        return name

    @classmethod
    def failure_status(cls, status: int = 1) -> int:
        """Notification programs retain the failed operation's exit status."""
        return status

    @classmethod
    def failed(cls, error: BaseException) -> NoReturn:
        sys.stderr.write(f"{error}\n")
        raise SystemExit(cls.failure_status())

    @classmethod
    def unbound(cls, record: Mapping[str, object]) -> None:
        """A session with no bus has no commander or fleet behavior to run."""

    @classmethod
    def owns_entry(cls, entry: Mapping[str, object]) -> bool:
        """Ownership survives deletion of a program and its console script. An untagged
        entry running only this package's hooks was written before the tag existed;
        install adopts it, so each hook keeps one entry."""
        match entry:
            case {"writer": writer} if writer == cls.writer:
                return True
            case {"hooks": [_, *_] as hooks}:
                return all(hook in cls.hooks() for hook in hooks)
        return False

    @classmethod
    @cache
    def hooks(cls) -> tuple[dict[str, str], ...]:
        """The hook every declared program installs."""
        return tuple(program.hook() for program in cls.members())

    @classmethod
    def hook(cls) -> dict[str, str]:
        return {"type": "command", "command": cls.command()}

    @classmethod
    def entry(cls) -> dict[str, object]:
        """This program's entry, marked so install can replace what it wrote."""
        return {"writer": cls.writer, "hooks": [cls.hook()]}


class PermissionHookProgram(ClaudeHookProgram, ABC):
    """Claude Code blocks a permission event only on its blocking exit code."""

    @classmethod
    def failure_status(cls, status: int = 1) -> int:
        return 2


class ClaudeToolHookProgram(ClaudeHookProgram, ABC):
    """A program Claude Code runs only on calls of the one tool it names."""

    tool: ClassVar[str]

    @classmethod
    def entry(cls) -> dict[str, object]:
        return {**super().entry(), "matcher": cls.tool}


@dataclass(frozen=True)
class Bash(HookRecord):
    """The Bash tool's input: one shell command line."""

    command: str


class HookRefused(ToolRefusedError):
    """An owned hook refusal, rendered as a denial rather than a hook crash."""


class BashRefusal(ClaudeToolHookProgram, PermissionHookProgram, ABC):
    """A program that refuses a Bash command before it runs, for every reason it finds;
    each member says only what it refuses and why."""

    events = ("PreToolUse",)
    tool = Bash.__name__
    headline: ClassVar[str]
    advice: ClassVar[str] = ""

    @classmethod
    @abstractmethod
    def reasons(cls, command: str) -> tuple[str, ...]:
        """Why this command line is refused; empty when it may run."""

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{cls.__module__}:main")

    @classmethod
    def binds(cls, hook: ClaudeToolHook) -> bool:
        """Whether the refusal holds in this hook's session; it holds in every session."""
        return True

    @classmethod
    def reasons_for(cls, hook: ClaudeToolHook) -> tuple[str, ...]:
        """A session-aware refusal receives the original hook, not a global context copy."""
        return cls.reasons(Bash.decode(hook.tool_input).command)

    @classmethod
    def main(cls) -> None:
        cls.judge(ClaudeToolHook.read())

    @classmethod
    def unbound(cls, record: Mapping[str, object]) -> None:
        cls.judge(ClaudeToolHook.decode(record))

    @classmethod
    def judge(cls, hook: ClaudeToolHook) -> None:
        if not cls.binds(hook):
            return
        try:
            reasons = cls.reasons_for(hook)
        except HookRefused as refused:
            reasons = (str(refused),)
        cls.refuse(hook, reasons)

    @classmethod
    def refuse(cls, hook: ClaudeToolHook, reasons: tuple[str, ...]) -> None:
        """Both local syntax and resident authorization use the same permission response."""
        if reasons:
            hook.respond(
                permissionDecision="deny",
                permissionDecisionReason=f"{cls.headline}: {'; '.join(reasons)}{cls.advice}",
            )
