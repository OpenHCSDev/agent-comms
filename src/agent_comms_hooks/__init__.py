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
from dataclasses import dataclass, fields
from functools import cache
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import ClassVar, Self, get_type_hints


@dataclass(frozen=True)
class HookRecord:
    """A JSON object Claude Code writes to a hook, decoded once by its declared fields."""

    @classmethod
    def decode(cls, record: Mapping[str, object]) -> Self:
        """A field Claude Code may omit declares its default; a required field it omits is a
        missing argument, refused by the constructor."""
        hints = get_type_hints(cls)
        return cls(
            **{
                declared.name: HookRecord.field(hints[declared.name], record[declared.name])
                for declared in fields(cls)
                if declared.name in record
            }
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


@dataclass(frozen=True)
class ClaudeHook(HookRecord):
    """What Claude Code writes to every hook's stdin."""

    session_id: str
    hook_event_name: str

    @classmethod
    def read(cls) -> Self:
        return cls.decode(json.load(sys.stdin))

    def respond(self, **fields: str) -> None:
        """Claude Code's answer to this event: its event name plus the given fields."""
        print(json.dumps({"hookSpecificOutput": {"hookEventName": self.hook_event_name, **fields}}))

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
    def stem(self) -> Path:
        """A session's deal and deck files sit beside its transcript, each with its own suffix."""
        return Path(self.transcript_path)

    @property
    def subagent_transcripts(self) -> Path:
        """Claude Code keeps each subagent's transcript in this directory beside the session's."""
        return Path(self.transcript_path).with_suffix("") / "subagents"

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
            program
            for program in ClaudeHookProgram.descendants()
            if not program.__abstractmethods__
        )

    @classmethod
    def descendants(cls) -> tuple[type[ClaudeHookProgram], ...]:
        return (cls, *(member for child in cls.__subclasses__() for member in child.descendants()))

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


class HookRefused(Exception):
    """An owned hook refusal, rendered as a denial rather than a hook crash."""


class BashRefusal(ClaudeToolHookProgram, ABC):
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
        hook = ClaudeToolHook.read()
        if not cls.binds(hook):
            return
        try:
            reasons = cls.reasons_for(hook)
        except HookRefused as refused:
            reasons = (str(refused),)
        if reasons:
            hook.respond(
                permissionDecision="deny",
                permissionDecisionReason=f"{cls.headline}: {'; '.join(reasons)}{cls.advice}",
            )
