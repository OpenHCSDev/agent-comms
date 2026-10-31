"""What a session's own transcript shows it did, read back to it in the owner's voice.

Tristan does not check the shape of an answer. He compares what was said with what was done and
quotes the difference. On 2026-10-31 claude-nra told him "Yes" about the rules it had just read and
then ended some fifty turns on "Skipped" with his requests open; the transcript held every one."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar


@dataclass
class TurnEnding:
    """The words one turn stopped on, and how many calls ran and changed something before them."""

    words: str
    acted: int


@dataclass
class SessionReceipts:
    """A fold over transcript records, oldest first. A reply followed by a call did not end its
    turn, so its words stay pending until the turn's end or the transcript's end confirms them."""

    calls: int = 0
    refused: int = 0
    endings: list[TurnEnding] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    running: dict[str, str] = field(default_factory=dict)
    acted: int = 0

    quoted_endings: ClassVar[int] = 3
    quoted_width: ClassVar[int] = 120
    reading_tool: ClassVar[str] = "Read"

    def said(self, words: tuple[str, ...]) -> None:
        self.pending.extend(words)

    def called(self, call: str, tool: str) -> None:
        self.calls += 1
        self.running[call] = tool
        self.pending.clear()

    def resulted(self, call: str, refused: bool) -> None:
        self.refused += refused
        if self.running.pop(call, self.reading_tool) != self.reading_tool and not refused:
            self.acted += 1

    def turn_ended(self) -> None:
        """The turn stopped on whatever the session last said; a turn that said nothing after
        its last call stopped on no words, and its calls carry over to the next ending."""
        if self.pending:
            self.endings.append(TurnEnding(self.pending[-1], self.acted))
            self.pending.clear()
            self.acted = 0

    def idle_turns(self) -> int:
        """How many of the newest turns in a row ended with nothing having run but reads."""
        idle = 0
        for ending in reversed(self.endings):
            if ending.acted:
                break
            idle += 1
        return idle

    def lines(self) -> tuple[str, ...]:
        """The receipts as he would read them out; a session with no record has none."""
        ended = tuple(self.quoted(ending) for ending in self.endings[-self.quoted_endings:])
        return (*(("Your last turns ended on:", *ended) if ended else ()),
                *((f"Of your last {self.calls} calls, {self.refused} were refused or failed.",)
                  if self.calls else ()))

    def quoted(self, ending: TurnEnding) -> str:
        first = next((line.strip() for line in ending.words.splitlines() if line.strip()), "")
        return f"> {first[:self.quoted_width]}"
