"""Keep a bound session's sources in front of it while it works.

Claude Code runs this after every tool call. The session start hook orders the sources read
once and deals them into a deck of short slices beside the transcript: the voice, the
commander's indictment, each followed rule's wake-up with its tell, and the recorded
failures, interleaved. Every `every` tool calls one slice goes
into context inline, the next one each time. Nothing repeats verbatim on every call, nothing
waits on a read the commander may skip, and the cadence counts tool calls, so no transcript
parse can stall it.

In the commander's session the same call carries the RE-ARM line while its bridge runs with no Monitor
reading it, since every other route to the commander goes through that dead reader, and the
heartbeat's board, which the bridge derives from Core whenever this hook asks.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import ClassVar

from agent_comms_hooks import ClaudeHookProgram
from agent_comms_hooks.read_gate import GatedCall
from agent_comms_hooks.skill import DeckReader, DeckStart


@dataclass(frozen=True)
class SliceReminder(DeckStart):
    """Every `every` tool calls, the next slice of the deck this session's role is dealt."""

    every: ClassVar[int] = 5

    def taken(self, reader: DeckReader) -> tuple[str, ...]:
        slices = self.slices(reader)
        count = self.count_call()
        if count % self.every:
            return ()
        return (slices[(count // self.every - 1) % len(slices)],)


@dataclass(frozen=True)
class DeafReminder(SliceReminder):
    """Beside the deck's slice, the RE-ARM line while the commander session's bridge has no
    Monitor reading it. The line rides the next tool call, never the bridge it reports dead."""

    def taken(self, reader: DeckReader) -> tuple[str, ...]:
        return super().taken(reader) + reader.deaf(self.session_id)


@dataclass(frozen=True)
class BoardReminder(DeafReminder):
    """New and changed current lines once, then one unchanged count per tool call.

    The bridge derives current lines from Core and owns their delivery evidence.
    """

    def taken(self, reader: DeckReader) -> tuple[str, ...]:
        return super().taken(reader) + reader.board(self.session_id)


class ReminderProgram(ClaudeHookProgram):
    events = ("PostToolUse",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    """A completed tool call first records what it read for the read gate, then takes its
    reminder; both decode the one record Claude Code sent."""
    record = json.load(sys.stdin)
    GatedCall.of(record).completed()
    hook = BoardReminder.decode(record)
    if texts := hook.take():
        hook.warn(*texts)


if __name__ == "__main__":
    main()
