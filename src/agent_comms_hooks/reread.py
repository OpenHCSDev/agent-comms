"""Order the commander to reread its sources once context has grown since the last reread.

Claude Code runs this after every tool call, so like every hook it imports only
the standard library. The session start hook writes the sources into parts
beside the transcript; this hook orders them read again every `every` tokens.
"""

from __future__ import annotations

import json
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
class CommanderReread(ClaudeTranscriptHook):
    """A reread is due once context grew `every` tokens past the last one."""

    every: ClassVar[int] = 100_000
    window: ClassVar[int] = 1_000_000
    """Transcript bytes searched from the end for the latest model reply."""

    @property
    def mark(self) -> Path:
        return Path(self.transcript_path).with_suffix(".reread.json")

    @property
    def parts(self) -> Path:
        return Path(self.transcript_path).with_suffix(".reread")

    def context_tokens(self) -> int:
        """The prompt size of the latest model reply; zero before the first one."""
        with open(self.transcript_path, "rb") as transcript:
            start = max(0, transcript.seek(0, 2) - self.window)
            transcript.seek(start)
            lines = transcript.read().splitlines()[1 if start else 0 :]
        for line in reversed(lines):
            if ModelReply.marker in line:
                return ModelReply.decode(json.loads(line)).message.usage.context
        return 0

    def due(self, tokens: int) -> bool:
        """Before the session start hook wrote the parts there is nothing to reread.

        Context smaller than the mark means a compaction dropped what was read, so the
        reread is due at once; otherwise a mark set before compaction is never reached.
        """
        if not self.mark.exists():
            return False
        last = RereadMark.decode(json.loads(self.mark.read_text()))
        return tokens < last.tokens or tokens - last.tokens >= self.every

    def write_parts(self) -> None:
        """The parts the session start hook wrote stay current for the session."""

    def reread(self) -> None:
        tokens = self.context_tokens()
        if not self.due(tokens):
            return
        self.write_parts()
        paths = sorted(self.parts.iterdir())
        self.mark.write_text(json.dumps(asdict(RereadMark(tokens))))
        self.warn(
            f"Read each of these {len(paths)} files in full with the Read tool before any other"
            " action: they are the indictment, the recorded failures, the commander skill, every"
            " packaged rule and the refactor-audit pattern catalog. A summary or memory of them"
            " does not count as reading them.\n" + "\n".join(map(str, paths))
        )


class RereadProgram(ClaudeHookProgram):
    events = ("PostToolUse",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    CommanderReread.read().reread()


if __name__ == "__main__":
    main()
