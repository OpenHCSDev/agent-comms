"""Remind the commander of its sources and its voice as context grows.

Claude Code runs this after every tool call, so like every hook it imports only
the standard library. The session start hook writes the sources into parts
beside the transcript; each reminder fires every `every` tokens of its own, and
all due reminders go out in one hook output. A sources order counts from the
reply after its parts were read, never from when it was sent.
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
class RereadOrder(HookRecord):
    """A sources order not yet read: the parts still unread, and the context of the reply
    that issued the latest part read. Its mark settles only at a later reply, so the reads
    themselves are counted as read instead of as growth toward the next order."""

    unread: list[str]
    read_in: int


@dataclass(frozen=True)
class ReadInput(HookRecord):
    """The Read tool's input, as Claude Code names the tool and its field."""

    file_path: str


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

    @property
    def drift(self) -> Path:
        """The commander's drift tells, one per line, written beside the transcript at start."""
        return Path(self.transcript_path).with_suffix(".drift.md")

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
    def message(self, tokens: int) -> str: ...

    def take(self, tokens: int) -> tuple[str, ...]:
        """The member's message when due, marking the context size it was given at."""
        if not self.due(tokens):
            return ()
        self.prepare()
        self.mark.write_text(json.dumps(asdict(RereadMark(tokens))))
        return (self.message(tokens),)

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
class ToolUseReminder(ContextCadence, ABC):
    """A reminder checked after every tool call; each direct subclass is one reminder."""

    tool_name: str
    tool_input: dict

    def read_paths(self) -> frozenset[str]:
        """The file this call read with the Read tool; none for every other tool."""
        if self.tool_name != "Read":
            return frozenset()
        return frozenset((ReadInput.decode(self.tool_input).file_path,))


@dataclass(frozen=True)
class SourcesOrder(ContextCadence, ABC):
    """The order to read every source part.

    Session start and the reread share its order file and its mark.
    """

    mark_suffix: ClassVar[str] = ".reread.json"

    @property
    def order(self) -> Path:
        return Path(self.transcript_path).with_suffix(".reread-order.json")

    def outstanding(self) -> RereadOrder:
        return RereadOrder.decode(json.loads(self.order.read_text()))

    def message(self, tokens: int) -> str:
        unread = self.outstanding().unread
        return (
            f"Read each of these {len(unread)} files in full with the Read tool before any other"
            " action: they are the indictment, the recorded failures, the commander skill, every"
            " packaged rule and the refactor-audit pattern catalog. A summary or memory of them"
            " does not count as reading them.\n" + "\n".join(unread)
        )

    def take(self, tokens: int) -> tuple[str, ...]:
        """An order names every part; its mark waits until they are read."""
        if not self.due(tokens):
            return ()
        self.prepare()
        unread = [str(path) for path in sorted(self.parts.iterdir())]
        self.order.write_text(json.dumps(asdict(RereadOrder(unread, -1))))
        return (self.message(tokens),)


@dataclass(frozen=True)
class CommanderReread(SourcesOrder, ToolUseReminder):
    """Orders the commander's sources read again, and settles each order once it is read."""

    every: ClassVar[int] = 100_000

    def take(self, tokens: int) -> tuple[str, ...]:
        """An outstanding order is repeated only when another action comes before its reads.

        Each part read crosses it off. After the last one, the mark is the context of the
        next reply, which holds the reads, so the next order comes `every` tokens of work later.
        """
        if not self.order.exists():
            return super().take(tokens)
        order = self.outstanding()
        read = self.read_paths() & frozenset(order.unread)
        if read:
            unread = [path for path in order.unread if path not in read]
            self.order.write_text(json.dumps(asdict(RereadOrder(unread, tokens))))
            return ()
        if order.unread:
            return (self.message(tokens),)
        if tokens != order.read_in:
            self.mark.write_text(json.dumps(asdict(RereadMark(tokens))))
            self.order.unlink()
        return ()


@dataclass(frozen=True)
class VoiceReminder(ToolUseReminder):
    """Puts the commander's voice back in fresh context, inline, far more often than a reread."""

    every: ClassVar[int] = 25_000
    mark_suffix: ClassVar[str] = ".voice.json"

    def message(self, tokens: int) -> str:
        return self.voice.read_text(encoding="utf-8")


@dataclass(frozen=True)
class DriftCheck(ToolUseReminder):
    """Two one-line drift tells, rotating with context size, far more often than the voice."""

    every: ClassVar[int] = 8_000
    mark_suffix: ClassVar[str] = ".drift.json"
    shown: ClassVar[int] = 2

    def message(self, tokens: int) -> str:
        tells = [line for line in self.drift.read_text(encoding="utf-8").splitlines() if line.strip()]
        start = tokens // self.every * self.shown
        return "Drift check:\n" + "\n".join(
            tells[(start + offset) % len(tells)] for offset in range(self.shown))


class RereadProgram(ClaudeHookProgram):
    events = ("PostToolUse",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    ContextCadence.remind(*ToolUseReminder.__subclasses__())


if __name__ == "__main__":
    main()
