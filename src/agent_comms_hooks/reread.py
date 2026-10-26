"""Keep the commander's sources in front of it while it works.

Claude Code runs this after every tool call. The session start hook orders the sources read
once and deals them into a deck of short slices beside the transcript: the voice, the
indictment and the rules' drift tells, interleaved. Every `every` tool calls one slice goes
into context inline, the next one each time. Nothing repeats verbatim on every call, nothing
waits on a read the commander may skip, and the cadence counts tool calls, so no transcript
parse can stall it.

The same call carries the RE-ARM line while this session's bridge runs with no Monitor
reading it, since every other route to the commander goes through that dead reader, and the
heartbeat's board, which the bridge derives from Core whenever this hook asks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms_hooks import ClaudeHookProgram, ClaudeTranscriptHook
from agent_comms_hooks.watch import WatchClaim


@dataclass(frozen=True)
class CommanderDeck(ClaudeTranscriptHook):
    """The slices the session start hook dealt beside the transcript, and the calls since."""

    @property
    def deck(self) -> Path:
        return Path(self.transcript_path).with_suffix(".deck.json")

    @property
    def calls(self) -> Path:
        """One byte per tool call since the deck was dealt. An append is atomic, so tool
        calls that run in parallel each count once."""
        return Path(self.transcript_path).with_suffix(".calls")


@dataclass(frozen=True)
class SliceReminder(CommanderDeck):
    """Every `every` tool calls, the next slice of the deck; only a commander session has one."""

    every: ClassVar[int] = 5

    def take(self) -> tuple[str, ...]:
        if not self.deck.exists():
            return ()
        with self.calls.open("ab") as calls:
            calls.write(b".")
            count = calls.tell()
        if count % self.every:
            return ()
        slices = json.loads(self.deck.read_text(encoding="utf-8"))
        return (slices[(count // self.every - 1) % len(slices)],)


@dataclass(frozen=True)
class DeafReminder(SliceReminder):
    """Beside the deck's slice, the RE-ARM line while this session's bridge has no Monitor
    reading it. The line rides the next tool call, never the bridge it reports dead."""

    def take(self) -> tuple[str, ...]:
        return super().take() + tuple(deaf.line() for deaf in WatchClaim(self.session_id).deaf())


@dataclass(frozen=True)
class BoardReminder(DeafReminder):
    """New and changed current lines once, then one unchanged count per tool call.

    The bridge derives current lines from Core and owns their delivery evidence.
    """

    def take(self) -> tuple[str, ...]:
        return super().take() + WatchClaim(self.session_id).state().board()


class ReminderProgram(ClaudeHookProgram):
    events = ("PostToolUse",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    hook = BoardReminder.read()
    if texts := hook.take():
        hook.warn("\n\n".join(texts))


if __name__ == "__main__":
    main()
