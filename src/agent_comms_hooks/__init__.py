"""Programs Claude Code runs as hooks, and what each declares about itself.

Claude Code runs a hook on every prompt or tool call it is registered for, and
importing agent_comms takes a second, so a hook's import path is the standard
library only. Each program declares the events it answers and, when it reads
one tool's input, that tool; the command that runs it is derived from the
installed console scripts. Install writes Claude Code's settings from these
declarations.
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


@dataclass(frozen=True)
class ToolInput(HookRecord):
    """The input of one tool, as Claude Code passes it to a hook on that tool."""

    tool: ClassVar[str]


@dataclass(frozen=True)
class BashInput(ToolInput):
    tool = "Bash"
    command: str


@dataclass(frozen=True)
class ClaudeBashHook(ClaudeHook):
    """A hook on a Bash tool call, carrying the command line Claude is about to run."""

    tool_input: BashInput


class ClaudeHookProgram(ABC):
    """A program Claude Code runs on each of `events`, whatever tool is involved."""

    events: ClassVar[tuple[str, ...]]

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
    def entry(cls) -> dict[str, object]:
        """This program's entry in a Claude Code hook event's list."""
        return {"hooks": [{"type": "command", "command": cls.command()}]}


class ClaudeToolHookProgram(ClaudeHookProgram):
    """A program that reads one tool's input, so Claude Code runs it for that tool only."""

    reads: ClassVar[type[ToolInput]]

    @classmethod
    def entry(cls) -> dict[str, object]:
        return {"matcher": cls.reads.tool, **super().entry()}
