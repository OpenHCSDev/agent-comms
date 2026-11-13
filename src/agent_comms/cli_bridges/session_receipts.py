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

    endings: list[TurnEnding] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    running: dict[str, str] = field(default_factory=dict)
    acted: int = 0
    idle_turns: int = 0
    """How many turns in a row have ended with nothing having run but reads, since the session
    last changed something or the owner last typed something."""

    reading_tool: ClassVar[str] = "Read"

    def said(self, words: tuple[str, ...]) -> None:
        self.pending.extend(words)

    def called(self, call: str, tool: str) -> None:
        self.running[call] = tool
        self.pending.clear()

    def resulted(self, call: str, refused: bool) -> None:
        if self.running.pop(call, self.reading_tool) != self.reading_tool and not refused:
            self.acted += 1
            # A call that changed something is the newest thing the session did, whether or
            # not the words that end its turn are written yet.
            self.idle_turns = 0

    def turn_ended(self) -> None:
        """The turn stopped on whatever the session last said; a turn that said nothing after
        its last call stopped on no words, and its calls carry over to the next ending."""
        if self.pending:
            self.endings.append(TurnEnding(self.pending[-1], self.acted))
            self.idle_turns = 0 if self.acted else self.idle_turns + 1
            self.pending.clear()
            self.acted = 0

    def spoken_to(self) -> None:
        """The owner typed something. He holds what the session did after his words against
        what it said, so a turn that ended before them is not in a row with the ones after:
        a session answering him turn after turn is talking with him, not idling."""
        self.idle_turns = 0
