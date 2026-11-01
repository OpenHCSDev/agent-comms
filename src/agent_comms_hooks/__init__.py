"""Programs Claude Code runs as hooks, and what each declares about itself.

Claude Code runs a hook on every prompt or tool call it is registered for, and
importing agent_comms takes a second, so a hook's import path is the standard
library only. SessionStart alone bootstraps Core; it does not run per tool.
Each program declares the events it answers; the command that runs it is derived
from the installed console scripts. Install writes Claude Code's settings from
these declarations.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import ClassVar, NoReturn, Self, get_type_hints

from .shell_commands import ConnectCommand


class ToolRefusedError(ValueError):
    """An intentional refusal safe to return as a tool answer, not a transport failure."""


@dataclass(frozen=True)
class HookRecord:
    """A JSON object Claude Code writes to a hook, decoded once by its declared fields."""

    @classmethod
    def decode(cls, record: Mapping[str, object], **context: object) -> Self:
        """Declared fields decode regardless of constructor position. Supplied resident
        context wins over foreign input; omitted required fields fail at construction."""
        hints = get_type_hints(cls)
        return cls(
            **{
                declared.name: HookRecord.field(hints[declared.name], record[declared.name])
                for declared in fields(cls)
                if declared.init and declared.name in record and declared.name not in context
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


class LaunchedPrompt:
    """The system prompt a Core launcher gave this Claude process (claude_launch). Claude Code
    passes its own environment to every hook, so each hook record carries the prompt the deal
    was delivered in; a record that does not declare the field ignores it."""

    variable: ClassVar[str] = "AGENT_COMMS_SYSTEM_PROMPT"
    key: ClassVar[str] = "system_prompt"

    @classmethod
    def carried(cls, record: Mapping[str, object]) -> dict[str, object]:
        """The hook record, with the launcher's prompt when this process was launched with one.
        The environment is foreign input, decoded here once."""
        prompt = os.environ.get(cls.variable, "")
        return {**record, cls.key: prompt} if prompt else dict(record)


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
    window: ClassVar[int] = 6_000_000  # saved request records are huge: 1 MB can hold no turn's end
    """Transcript bytes read from the end."""

    @staticmethod
    def raised_by_subagent(record: Mapping[str, object]) -> bool:
        """The boundary test: Claude Code names a subagent's event by its agent_id."""
        return "agent_id" in record

    @property
    def session_stem(self) -> Path:
        """The main agent's transcript, which every event names, its subagents' included."""
        return Path(self.transcript_path)

    @property
    def stem(self) -> Path:
        """A session's deal and deck files sit beside its transcript, each with its own suffix."""
        return self.session_stem

    @property
    def subagent_transcripts(self) -> Path:
        """Claude Code keeps each subagent's transcript in this directory beside the session's."""
        return self.session_stem.with_suffix("") / "subagents"

    def tail(self) -> list[bytes]:
        """Transcript lines in the last `window` bytes, oldest first; a line the window cuts is dropped."""
        with open(self.transcript_path, "rb") as transcript:
            start = max(0, transcript.seek(0, 2) - self.window)
            transcript.seek(start)
            return transcript.read().splitlines()[1 if start else 0 :]


@dataclass(frozen=True)
class ClaudeSubagentHook(ClaudeTranscriptHook):
    """An event a subagent raised. It shares its session's id and transcript path, but its
    deal and deck sit beside its own transcript, so its reads, its audit and its compactions
    never touch the main agent's."""

    agent_id: SubagentId = field(kw_only=True)

    @property
    def stem(self) -> Path:
        return self.subagent_transcripts / f"agent-{self.agent_id}.jsonl"


@dataclass(frozen=True)
class ClaudeToolHook(ClaudeTranscriptHook):
    """What Claude Code adds for a tool-call event: the tool and its input."""

    tool_name: str
    tool_input: dict


class ClaudeHookProgram(ABC):
    """A program Claude Code runs on each of `events`, whatever tool is involved."""

    events: ClassVar[tuple[str, ...]] = ()
    writer: ClassVar[str] = "agent-comms"

    @classmethod
    def installation_members(cls) -> tuple[type[ClaudeHookProgram], ...]:
        """A public program installs itself; a resident handler does not install a copy."""
        return (cls,) if cls.events else ()

    @classmethod
    def descendants(cls) -> tuple[type[ClaudeHookProgram], ...]:
        # Multiple capabilities reach the same declaration through several paths, not several programs.
        descendants = (member for child in cls.__subclasses__() for member in child.descendants())
        return tuple(dict.fromkeys((cls, *descendants)))

    @classmethod
    @abstractmethod
    def main(cls) -> None:
        """Run this program at its process boundary."""

    @classmethod
    def wire_name(cls) -> str:
        """The declared program owns its RPC identity, including under ``python -m``."""
        return cls.__qualname__

    @classmethod
    def entrypoint(cls) -> str:
        """Installation derives the callable target; runtime never reads distributions."""
        return f"{cls.__module__}:{cls.__qualname__}.{cls.main.__name__}"

    @classmethod
    def failure_status(cls, status: int = 1) -> int:
        """Notification programs retain the failed operation's exit status."""
        return status

    @classmethod
    def failed(cls, error: BaseException) -> NoReturn:
        traceback.print_exception(error, file=sys.stderr)
        raise SystemExit(cls.failure_status())

    @classmethod
    def unbound(cls, record: Mapping[str, object]) -> None:
        """A session with no bus has no commander or fleet behavior to run."""

    @classmethod
    def absent(cls, record: Mapping[str, object], error: BaseException) -> None:
        """An enrolled session does not silently become an unbound one."""
        cls.failed(error)

    @classmethod
    def owns_entry(cls, entry: Mapping[str, object], installed_hooks: tuple[dict[str, str], ...]) -> bool:
        """Ownership survives deletion of a program and its console script. An untagged
        entry running only this package's hooks was written before the tag existed;
        install adopts it, so each hook keeps one entry."""
        match entry:
            case {"writer": writer} if writer == cls.writer:
                return True
            case {"hooks": [_, *_] as hooks}:
                return all(hook in installed_hooks for hook in hooks)
        return False

    @classmethod
    def hook(cls, command: str) -> dict[str, str]:
        return {"type": "command", "command": command}

    @classmethod
    def entry(cls, command: str) -> dict[str, object]:
        """This program's entry, marked so install can replace what it wrote."""
        return {"writer": cls.writer, "hooks": [cls.hook(command)]}


class PermissionHookProgram(ClaudeHookProgram, ABC):
    """Claude Code blocks a permission event only on its blocking exit code."""

    @classmethod
    def failure_status(cls, status: int = 1) -> int:
        return 2

    @classmethod
    def absent(cls, record: Mapping[str, object], error: BaseException) -> None:
        """Only a standalone connect command can repair the missing resident owner."""
        hook = ClaudeToolHook.decode(record)
        if hook.tool_name == Bash.__name__ and ConnectCommand.permits(Bash.decode(hook.tool_input).command):
            cls.unbound(record)
            return
        cls.failed(error)


class ClaudeToolHookProgram(ClaudeHookProgram, ABC):
    """A program Claude Code runs only on calls of the one tool it names."""

    tool: ClassVar[str]

    @classmethod
    def entry(cls, command: str) -> dict[str, object]:
        return {**super().entry(command), "matcher": cls.tool}


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
