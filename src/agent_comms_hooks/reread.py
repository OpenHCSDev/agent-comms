"""Remind the commander of its sources and its voice as context grows.

Claude Code runs this after every tool call, so like every hook it imports only
the standard library. The session start hook writes the sources into parts
beside the transcript; each reminder fires every `every` tokens of its own, and
all due reminders go out in one hook output.
"""

from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms_hooks import ClaudeHookProgram, ClaudeTranscriptHook, HookRecord


@dataclass(frozen=True)
class ModelUsage(HookRecord):
    """The prompt size Claude Code records on each model reply."""

    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int

    @property
    def context(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


@dataclass(frozen=True)
class ModelMessage(HookRecord):
    usage: ModelUsage


@dataclass(frozen=True)
class ModelReply(HookRecord):
    """A transcript line holding a model reply; Claude Code writes its type key unspaced."""

    message: ModelMessage
    marker: ClassVar[bytes] = b'"type":"assistant"'


@dataclass(frozen=True)
class RereadMark(HookRecord):
    """The context size at the last reread of one session."""

    tokens: int


@dataclass(frozen=True)
class ContextCadence(ClaudeTranscriptHook, ABC):
    """A reminder due once context grew `every` tokens past its own mark, or shrank below it."""

    every: ClassVar[int]
    mark_suffix: ClassVar[str]

    @property
    def mark(self) -> Path:
        return Path(self.transcript_path).with_suffix(self.mark_suffix)

    @property
    def voice(self) -> Path:
        """The commander's voice, written by the session start hook beside the transcript."""
        return Path(self.transcript_path).with_suffix(".voice.md")

    def context_tokens(self) -> int:
        """The prompt size of the latest model reply; zero before the first one."""
        for line in reversed(self.tail()):
            if ModelReply.marker in line:
                return ModelReply.decode(json.loads(line)).message.usage.context
        return 0

    def due(self, tokens: int) -> bool:
        """Only a commander session, whose start hook wrote the parts, is reminded.

        Context smaller than the mark means a compaction dropped what was read, so the
        reminder is due at once; otherwise a mark set before compaction is never reached.
        """
        if not self.parts.exists():
            return False
        if not self.mark.exists():
            return True
        last = RereadMark.decode(json.loads(self.mark.read_text()))
        return tokens < last.tokens or tokens - last.tokens >= self.every

    def prepare(self) -> None:
        """Nothing to write before the message unless a member needs it."""

    @abstractmethod
    def message(self) -> str: ...

    def take(self, tokens: int) -> tuple[str, ...]:
        """The member's message when due, marking the context size it was given at."""
        if not self.due(tokens):
            return ()
        self.prepare()
        self.mark.write_text(json.dumps(asdict(RereadMark(tokens))))
        return (self.message(),)

    @staticmethod
    def remind(*members: type[ContextCadence]) -> None:
        """One hook output carrying every due member's message."""
        record = json.load(sys.stdin)
        hooks = [member.decode(record) for member in members]
        tokens = hooks[0].context_tokens()
        messages = [message for hook in hooks for message in hook.take(tokens)]
        if messages:
            hooks[0].warn("\n\n".join(messages))


@dataclass(frozen=True)
class CommanderReread(ContextCadence):
    """Orders the commander's sources read again."""

    every: ClassVar[int] = 100_000
    mark_suffix: ClassVar[str] = ".reread.json"

    def message(self) -> str:
        paths = sorted(self.parts.iterdir())
        return (
            f"Read each of these {len(paths)} files in full with the Read tool before any other"
            " action: they are the indictment, the recorded failures, the commander skill, every"
            " packaged rule and the refactor-audit pattern catalog. A summary or memory of them"
            " does not count as reading them.\n" + "\n".join(map(str, paths))
        )


@dataclass(frozen=True)
class VoiceReminder(ContextCadence):
    """Puts the commander's voice back in fresh context, inline, far more often than a reread."""

    every: ClassVar[int] = 25_000
    mark_suffix: ClassVar[str] = ".voice.json"

    def message(self) -> str:
        return self.voice.read_text(encoding="utf-8")


class RereadProgram(ClaudeHookProgram):
    events = ("PostToolUse",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    ContextCadence.remind(CommanderReread, VoiceReminder)


if __name__ == "__main__":
    main()
