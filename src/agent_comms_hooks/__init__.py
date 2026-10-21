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
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import ClassVar, Self, get_type_hints


@dataclass(frozen=True)
class HookRecord:
    """A JSON object Claude Code writes to a hook, decoded once by its declared fields."""

    @classmethod
    def decode(cls, record: Mapping[str, object]) -> Self:
        hints = get_type_hints(cls)
        return cls(
            **{
                declared.name: HookRecord.field(hints[declared.name], record[declared.name])
                for declared in fields(cls)
            }
        )

    @staticmethod
    def field(declared: type, value: object) -> object:
        """A nested record is decoded by its own fields; a JSON scalar is kept as written."""
        if isinstance(value, Mapping) and issubclass(declared, HookRecord):
            return declared.decode(value)
        return value


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

    def warn(self, reason: str) -> None:
        self.respond(additionalContext=reason)


@dataclass(frozen=True)
class ClaudeToolHook(ClaudeHook):
    """What Claude Code adds for a tool-call event: the tool and its input."""

    tool_name: str
    tool_input: dict


@dataclass(frozen=True)
class ClaudeTranscriptHook(ClaudeHook):
    """A hook that reads the session's transcript, which Claude Code names on every event."""

    transcript_path: str
    window: ClassVar[int] = 1_000_000
    """Transcript bytes read from the end."""

    @property
    def parts(self) -> Path:
        """The commander's sources, written by the session start hook beside the transcript;
        they exist only in a commander session."""
        return Path(self.transcript_path).with_suffix(".reread")

    def tail(self) -> list[bytes]:
        """Transcript lines in the last `window` bytes, oldest first; a line the window cuts is dropped."""
        with open(self.transcript_path, "rb") as transcript:
            start = max(0, transcript.seek(0, 2) - self.window)
            transcript.seek(start)
            return transcript.read().splitlines()[1 if start else 0 :]


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
        """Ownership survives deletion of a program and its console script."""
        return entry.get("writer") == cls.writer

    @classmethod
    def entry(cls) -> dict[str, object]:
        """This program's entry, marked so install can replace what it wrote."""
        return {"writer": cls.writer, "hooks": [{"type": "command", "command": cls.command()}]}


class ClaudeToolHookProgram(ClaudeHookProgram, ABC):
    """A program Claude Code runs only on calls of the one tool it names."""

    tool: ClassVar[str]

    @classmethod
    def entry(cls) -> dict[str, object]:
        return {**super().entry(), "matcher": cls.tool}
